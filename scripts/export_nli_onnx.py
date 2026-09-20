"""Export the NLI cross-encoder to int8 ONNX. Runs in the Docker builder stage.

Heavy dependencies (torch, optimum) are needed here and only here — the runtime
image never sees them.

    python scripts/export_nli_onnx.py --output /models/nli-onnx
"""
import argparse
import shutil
from pathlib import Path

DEFAULT_MODEL = "cross-encoder/nli-deberta-v3-base"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output", default="/models/nli-onnx")
    parser.add_argument("--no-quantize", action="store_true")
    args = parser.parse_args()

    from optimum.onnxruntime import ORTModelForSequenceClassification, ORTQuantizer
    from optimum.onnxruntime.configuration import AutoQuantizationConfig
    from transformers import AutoTokenizer

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f"{output.name}-fp32"

    print(f"[export] Descargando y exportando {args.model} a ONNX…")
    model = ORTModelForSequenceClassification.from_pretrained(args.model, export=True)
    tokenizer = AutoTokenizer.from_pretrained(args.model)
    model.save_pretrained(staging)
    tokenizer.save_pretrained(staging)

    if args.no_quantize:
        shutil.copytree(staging, output, dirs_exist_ok=True)
    else:
        print("[export] Cuantizando a int8 (dinámica, AVX2)…")
        quantizer = ORTQuantizer.from_pretrained(staging)
        quantizer.quantize(
            save_dir=output,
            quantization_config=AutoQuantizationConfig.avx2(is_static=False, per_channel=True),
        )
        tokenizer.save_pretrained(output)
        # nli.py loads a fixed filename; normalise whatever the quantizer emitted.
        quantized = next(output.glob("*quantized*.onnx"), None)
        if quantized and quantized.name != "model.onnx":
            target = output / "model.onnx"
            target.exists() and target.unlink()
            quantized.rename(target)

    shutil.rmtree(staging, ignore_errors=True)
    for stale in output.glob("*.onnx"):
        if stale.name != "model.onnx":
            stale.unlink()

    total = sum(f.stat().st_size for f in output.rglob("*") if f.is_file())
    print(f"[export] Listo en {output} ({total / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
