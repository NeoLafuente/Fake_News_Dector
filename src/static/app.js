document.addEventListener("DOMContentLoaded", () => {
    // UI Elements
    const tabs = document.querySelectorAll(".tab-btn");
    const sections = document.querySelectorAll(".input-section");
    const fileInput = document.getElementById("media-file");
    const fileNameDisplay = document.getElementById("file-name-display");
    const btnTranscribe = document.getElementById("btn-transcribe");
    const btnFactcheck = document.getElementById("btn-factcheck");
    const loadingState = document.getElementById("loading-state");
    const resultsSection = document.getElementById("results-section");
    const transcriptContent = document.getElementById("transcript-content");
    const factsContainer = document.getElementById("facts-container");
    const factsList = document.getElementById("facts-list");
    const loadingText = document.getElementById("loading-text");

    // Audio Recording Elements
    const btnRecord = document.getElementById("btn-record");
    const recordStatus = document.getElementById("record-status");
    const audioPlayback = document.getElementById("audio-playback");
    const recordIcon = document.getElementById("record-icon");
    const recordText = document.getElementById("record-text");

    let activeInputType = "url"; // 'url', 'file', or 'record'
    let mediaRecorder;
    let audioChunks = [];
    let recordedAudioBlob = null;

    // Tab Switching
    tabs.forEach(tab => {
        tab.addEventListener("click", () => {
            tabs.forEach(t => t.classList.remove("active"));
            sections.forEach(s => s.classList.remove("active"));
            
            tab.classList.add("active");
            if (tab.dataset.tab === "url-tab") {
                activeInputType = "url";
            } else if (tab.dataset.tab === "file-tab") {
                activeInputType = "file";
            } else if (tab.dataset.tab === "record-tab") {
                activeInputType = "record";
            }
            document.getElementById(tab.dataset.tab).classList.add("active");
        });
    });

    // File Input Name display
    fileInput.addEventListener("change", (e) => {
        if(e.target.files.length > 0) {
            fileNameDisplay.textContent = e.target.files[0].name;
            fileNameDisplay.style.color = "#ffffff";
        } else {
            fileNameDisplay.textContent = "Click or drag an Audio/Video file";
            fileNameDisplay.style.color = "var(--text-secondary)";
        }
    });

    // Audio Recording Logic
    btnRecord.addEventListener("click", async (e) => {
        e.preventDefault();
        
        if (!mediaRecorder || mediaRecorder.state === "inactive") {
            try {
                // Check if browser supports mediaDevices
                if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                    throw new Error("Tu navegador no soporta grabación o estás usando HTTP sin localhost. Usa HTTPS o abrelo en localhost.");
                }

                recordStatus.textContent = "Solicitando permisos de micrófono...";
                recordStatus.style.color = "var(--text-secondary)";

                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                mediaRecorder = new MediaRecorder(stream);
                audioChunks = [];

                mediaRecorder.ondataavailable = event => {
                    if (event.data.size > 0) {
                        audioChunks.push(event.data);
                    }
                };

                mediaRecorder.onstop = () => {
                    // Save as closest possible mimeType that is acceptable.
                    // Instead of enforcing 'audio/webm', let browser use its default audio mimeType.
                    const mimeType = mediaRecorder.mimeType || 'audio/webm';
                    recordedAudioBlob = new Blob(audioChunks, { type: mimeType });
                    
                    const audioUrl = URL.createObjectURL(recordedAudioBlob);
                    audioPlayback.src = audioUrl;
                    audioPlayback.style.display = "block";
                    recordStatus.textContent = "Recording finished. Ready to process.";
                    recordStatus.style.color = "var(--secondary)";
                    
                    // Stop all tracks to release microphone
                    stream.getTracks().forEach(track => track.stop());
                };

                mediaRecorder.start();
                recordIcon.textContent = "⏹️";
                recordText.textContent = "Stop Recording";
                btnRecord.style.borderColor = "var(--contradiction-border)";
                recordStatus.textContent = "Recording in progress...";
                recordStatus.style.color = "var(--contradiction-border)";
                audioPlayback.style.display = "none";
                recordedAudioBlob = null;

            } catch (err) {
                console.error("Error accessing microphone:", err);
                alert("Error con el micrófono: " + err.message);
                recordStatus.textContent = "Error de micrófono";
                recordStatus.style.color = "var(--contradiction-border)";
            }
        } else if (mediaRecorder.state === "recording") {
            mediaRecorder.stop();
            recordIcon.textContent = "🔴";
            recordText.textContent = "Record Again";
            btnRecord.style.borderColor = "transparent";
        }
    });

    // Session countdown
    const countdownEl = document.getElementById("session-countdown");
    const btnLogout = document.getElementById("btn-logout");
    let secondsLeft = 0;

    async function loadSessionState() {
        try {
            const state = await (await fetch("/auth/state", { credentials: "same-origin" })).json();
            if (!state.has_session || !state.web_enabled) { window.location.href = "/"; return; }
            secondsLeft = state.seconds_left;
        } catch (e) {
            console.error("No se pudo leer el estado de sesión", e);
        }
    }

    setInterval(() => {
        if (secondsLeft <= 0) return;
        secondsLeft -= 1;
        const mm = String(Math.floor(secondsLeft / 60)).padStart(2, "0");
        const ss = String(secondsLeft % 60).padStart(2, "0");
        countdownEl.textContent = `${mm}:${ss}`;
        if (secondsLeft <= 0) window.location.href = "/";
    }, 1000);

    // Re-sync with the server so a revoked session is noticed quickly.
    setInterval(loadSessionState, 20000);
    loadSessionState();

    btnLogout.addEventListener("click", async () => {
        await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
        window.location.href = "/";
    });

    // Action Handlers
    btnTranscribe.addEventListener("click", () => submitJob("/transcribe_only", "Extracting Audio & Transcribing..."));
    btnFactcheck.addEventListener("click", () => submitJob("/process_url", "Agents are extracting facts and evaluating truthfulness..."));

    async function submitJob(endpoint, loadingMsg) {
        // Validation
        const urlValue = document.getElementById("media-url").value;
        const fileValue = fileInput.files[0];

        if (activeInputType === "url" && !urlValue) {
            alert("Please paste a valid URL");
            return;
        }
        if (activeInputType === "file" && !fileValue) {
            alert("Please select a file to upload");
            return;
        }
        if (activeInputType === "record" && !recordedAudioBlob) {
            alert("Please record some audio first");
            return;
        }

        // Prepare Data
        const formData = new FormData();
        if (activeInputType === "url") {
            formData.append("url", urlValue);
        } else if (activeInputType === "file") {
            formData.append("file", fileValue);
        } else if (activeInputType === "record") {
            // Give it a generic filename so the backend can process it as an uploaded file
            formData.append("file", recordedAudioBlob, "recorded_audio.webm");
        }

        // UI Updates
        loadingText.textContent = loadingMsg;
        loadingState.classList.remove("hidden");
        resultsSection.classList.add("hidden");
        factsContainer.classList.add("hidden");
        btnTranscribe.disabled = true;
        btnFactcheck.disabled = true;

        try {
            const response = await fetch(endpoint, {
                method: "POST",
                body: formData,
                credentials: "same-origin"
            });

            // The session may have expired or been revoked mid-request; the
            // gate page is the only sensible place to land.
            if (response.status === 401 || response.status === 503) {
                window.location.href = "/";
                return;
            }

            const data = await response.json();

            if (!response.ok) {
                throw new Error(data.detail || "Server error occurred");
            }

            renderResults(data, endpoint === "/process_url");

        } catch (error) {
            alert(`Error: ${error.message}`);
        } finally {
            loadingState.classList.add("hidden");
            btnTranscribe.disabled = false;
            btnFactcheck.disabled = false;
        }
    }

    function renderResults(data, showFacts) {
        resultsSection.classList.remove("hidden");
        
        // Render Transcript
        transcriptContent.textContent = data.transcript || "No transcript returned.";

        // Render Facts
        if (showFacts && data.results && data.results.length > 0) {
            factsContainer.classList.remove("hidden");
            factsList.innerHTML = "";

            data.results.forEach(fact => {
                const item = document.createElement("div");
                item.className = `fact-item ${fact.label}`;
                
                let urlsHtml = "<p style='font-size: 0.9rem; color: var(--text-secondary);'>No reliable sources found.</p>";
                if (fact.urls && fact.urls.length > 0) {
                    urlsHtml = '<ul style="margin-top: 5px; list-style: none; padding-left: 0; display: flex; flex-direction: column; gap: 5px;">';
                    fact.urls.forEach((url, i) => {
                       urlsHtml += `<li><a href="${url}" target="_blank" style="color: var(--primary); text-decoration: none; font-size: 0.85rem; word-break: break-all;">🔗 Source ${i+1}: ${url}</a></li>`;
                    });
                    urlsHtml += '</ul>';
                }

                item.innerHTML = `
                    <div class="fact-claim">"${fact.claim}"</div>
                    <div class="fact-snippet" style="margin-top: 15px; background: rgba(0,0,0,0.2); padding: 10px; border-radius: 8px;">
                        <strong style="font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-secondary);">Sources Evaluated</strong>
                        ${urlsHtml}
                    </div>
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 15px;">
                        <span class="fact-badge">${fact.label}</span>
                        <span style="font-size: 0.8rem; color: var(--text-secondary);">
                            ${fact.confidence ? `Confidence ${(fact.confidence * 100).toFixed(0)}%` : "Aggregated from search results"}
                        </span>
                    </div>
                `;
                factsList.appendChild(item);
            });
        }
    }
});
