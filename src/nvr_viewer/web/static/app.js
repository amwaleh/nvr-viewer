// NVR Viewer Frontend Application

const API = '';  // Same origin

const escapeHtml = (value) => String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');

class NVRApp {
    constructor() {
        this.cameras = [];
        this.activeTab = 'cameras';
        this.status = null;
        this.events = [];
        this.statusInterval = null;
        this.eventsInterval = null;
        this.feedState = new Map();
        this.init();
    }

    async init() {
        this.cameraOrder = JSON.parse(localStorage.getItem('nvr-camera-order') || '[]');
        this.cameraPage = 0;
        this.camerasPerPage = 4;
        this.setupEventListeners();
        await Promise.all([
            this.loadCameras(),
            this.loadRecordings(),
            this.loadEvents(),
            this.loadStatus(),
            this.loadDetectionSettings(),
            this.loadStorageSettings(),
        ]);
        this.startStatusPolling();
        this.startEventsPolling();
    }

    // --- API Calls ---
    async api(method, path, body = null) {
        const opts = { method, headers: {} };
        if (body !== null) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }

        const resp = await fetch(`${API}${path}`, opts);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({ detail: resp.statusText }));
            throw new Error(err.detail || 'API error');
        }

        return resp.json();
    }

    async loadCameras() {
        try {
            const cameras = await this.api('GET', '/api/cameras');
            // Apply saved order
            if (this.cameraOrder.length) {
                cameras.sort((a, b) => {
                    const ai = this.cameraOrder.indexOf(a.id);
                    const bi = this.cameraOrder.indexOf(b.id);
                    if (ai === -1 && bi === -1) return 0;
                    if (ai === -1) return 1;
                    if (bi === -1) return -1;
                    return ai - bi;
                });
            }
            const nextFeedState = new Map();
            cameras.forEach((camera) => {
                nextFeedState.set(camera.id, this.feedState.has(camera.id) ? this.feedState.get(camera.id) : true);
            });
            this.feedState = nextFeedState;
            this.cameras = cameras;
            this.renderCameraGrid();
            this.renderCameraList();
            this.populateCameraSelect();
            if (this.events.length) {
                this.renderEvents(this.events);
            }
            this.updateRefreshTime();
        } catch (error) {
            this.showToast(`Failed to load cameras: ${error.message}`, 'error');
        }
    }

    async scanNetwork() {
        const scanBtn = document.getElementById('scan-network-btn');
        const defaultView = document.getElementById('cameras-default-view');
        const scanView = document.getElementById('cameras-scan-view');
        const resultsContainer = document.getElementById('scan-results');

        // Switch to scan view
        if (defaultView) defaultView.style.display = 'none';
        if (scanView) scanView.style.display = 'block';

        // Show loading state
        if (scanBtn) {
            scanBtn.disabled = true;
            scanBtn.innerHTML = '<span class="spinner"></span> Scanning...';
        }
        if (resultsContainer) {
            resultsContainer.innerHTML = `
                <div style="display:flex;align-items:center;gap:12px;padding:20px;justify-content:center;color:#aaa;">
                    <div class="spinner" style="width:24px;height:24px;"></div>
                    <span>Scanning network for cameras...</span>
                </div>`;
        }

        try {
            const cameras = await this.api('GET', '/api/scan');
            this.renderScanResults(cameras);
            this.showToast(`Scan complete: ${cameras.length} device(s) found.`, 'success');
        } catch (error) {
            if (resultsContainer) {
                resultsContainer.innerHTML = '<div class="empty-inline">Scan failed. Try again.</div>';
            }
            this.showToast(`Network scan failed: ${error.message}`, 'error');
        } finally {
            if (scanBtn) {
                scanBtn.disabled = false;
                scanBtn.innerHTML = 'Scan Network';
            }
        }
    }

    showCamerasDefaultView() {
        const defaultView = document.getElementById('cameras-default-view');
        const scanView = document.getElementById('cameras-scan-view');
        if (defaultView) defaultView.style.display = 'block';
        if (scanView) scanView.style.display = 'none';
    }

    async addCamera(data) {
        try {
            const result = await this.api('POST', '/api/cameras', data);
            this.showToast(result.message || 'Camera added.', 'success');
            await Promise.all([this.loadCameras(), this.loadStatus()]);
            this.switchTab('cameras');
        } catch (error) {
            this.showToast(`Unable to add camera: ${error.message}`, 'error');
        }
    }

    async startStream(id) {
        try {
            await this.api('POST', `/api/stream/${id}/start`);
            this.feedState.set(id, true);
            this.renderCameraGrid();
            this.renderCameraList();
            this.showToast(`Starting stream for camera ${id}.`, 'info');
            await Promise.all([this.loadCameras(), this.loadStatus()]);
        } catch (error) {
            this.showToast(`Unable to start stream: ${error.message}`, 'error');
        }
    }

    async stopStream(id) {
        try {
            await this.api('POST', `/api/stream/${id}/stop`);
            this.feedState.set(id, false);
            this.renderCameraGrid();
            this.renderCameraList();
            this.showToast(`Stream stopped for camera ${id}.`, 'success');
            await Promise.all([this.loadCameras(), this.loadStatus()]);
        } catch (error) {
            this.showToast(`Unable to stop stream: ${error.message}`, 'error');
        }
    }

    async toggleRecording(id) {
        const camera = this.cameras.find((item) => item.id === id);
        const streamState = this.getCameraState(camera);
        const action = streamState.recording ? 'stop' : 'start';

        try {
            const result = await this.api('POST', `/api/record/${id}/${action}`);
            this.showToast(result.message || `Recording ${action}ed.`, streamState.recording ? 'info' : 'success');
            await Promise.all([this.loadStatus(), this.loadRecordings()]);
        } catch (error) {
            this.showToast(`Recording action failed: ${error.message}`, 'error');
        }
    }

    async takeSnapshot(id) {
        try {
            const resp = await fetch(`${API}/api/snapshot/${id}`);
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({ detail: resp.statusText }));
                throw new Error(err.detail || 'Snapshot failed');
            }

            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = `snapshot_${id}_${Date.now()}.jpg`;
            document.body.appendChild(link);
            link.click();
            link.remove();
            URL.revokeObjectURL(url);
            this.showToast('Snapshot downloaded.', 'success');
        } catch (error) {
            this.showToast(`Snapshot failed: ${error.message}`, 'error');
        }
    }

    async loadRecordings() {
        try {
            const files = await this.api('GET', '/api/recordings');
            this.renderRecordings(files);
            this.updateRefreshTime();
        } catch (error) {
            this.showToast(`Failed to load recordings: ${error.message}`, 'error');
        }
    }

    async loadSDCardFiles(cameraId) {
        if (!cameraId) {
            this.renderSDCardFiles(null);
            return;
        }

        try {
            const data = await this.api('GET', `/api/sdcard/${cameraId}`);
            this.renderSDCardFiles(data);
            this.updateRefreshTime();
        } catch (error) {
            this.showToast(`Failed to load SD card files: ${error.message}`, 'error');
        }
    }

    async downloadSDFile(cameraId, remotePath) {
        try {
            const resp = await fetch(`${API}/api/sdcard/${cameraId}/download?remote_path=${encodeURIComponent(remotePath)}`, {
                method: 'POST',
            });

            if (!resp.ok) {
                const err = await resp.json().catch(() => ({ detail: resp.statusText }));
                throw new Error(err.detail || 'Download failed');
            }

            const result = await resp.json();
            this.showToast(result.message || 'SD card file downloaded.', 'success');
            await this.loadRecordings();
        } catch (error) {
            this.showToast(`SD card download failed: ${error.message}`, 'error');
        }
    }

    async loadEvents(type = null) {
        const selectedType = type !== null ? type : (document.getElementById('event-type-filter')?.value || '');
        const query = selectedType ? `?detection_type=${encodeURIComponent(selectedType)}` : '';

        try {
            const resp = await this.api('GET', `/api/events${query}`);
            const events = resp.events || resp;
            this.events = events;
            this.renderEvents(events);
            this.updateRefreshTime();
        } catch (error) {
            this.showToast(`Failed to load events: ${error.message}`, 'error');
        }
    }

    async loadStatus() {
        try {
            const status = await this.api('GET', '/api/status');
            this.renderStatus(status);
            this.updateConnectionState(true, 'Connected to NVR backend');
            this.updateRefreshTime();
        } catch (error) {
            this.updateConnectionState(false, 'Backend unavailable');
            this.showToast(`Status check failed: ${error.message}`, 'error');
        }
    }

    // --- UI Rendering ---
    renderCameraGrid() {
        const container = document.getElementById('live-camera-grid');
        if (!container) return;

        if (!this.cameras.length) {
            container.innerHTML = '<div class="empty-state">No cameras — click Scan or Add Camera</div>';
            return;
        }

        const total = this.cameras.length;
        const perPage = this.camerasPerPage;
        const totalPages = Math.ceil(total / perPage);
        if (this.cameraPage >= totalPages) this.cameraPage = totalPages - 1;
        if (this.cameraPage < 0) this.cameraPage = 0;
        const start = this.cameraPage * perPage;
        const visibleCameras = this.cameras.slice(start, start + perPage);

        // Stop streams for cameras NOT on the current page
        this.cameras.forEach(cam => {
            const isVisible = visibleCameras.some(v => v.id === cam.id);
            const feedImg = document.querySelector(`img.feed-frame[src="/api/stream/${cam.id}"]`);
            if (!isVisible && feedImg) {
                feedImg.src = '';  // Stop loading the stream
            }
        });

        // Pagination header
        const pagHeader = total > perPage ? `
            <div class="camera-page-controls">
                <button class="btn-ghost" ${this.cameraPage === 0 ? 'disabled' : ''} data-action="cam-page-prev">← Prev</button>
                <span class="camera-page-info">Page ${this.cameraPage + 1} of ${totalPages} · Showing ${start + 1}–${Math.min(start + perPage, total)} of ${total} cameras</span>
                <button class="btn-ghost" ${this.cameraPage >= totalPages - 1 ? 'disabled' : ''} data-action="cam-page-next">Next →</button>
            </div>` : '';

        // Camera cards with drag support
        const cards = visibleCameras.map((camera) => {
            const state = this.getCameraState(camera);
            const enabled = this.feedState.get(camera.id) !== false;
            const streamAction = enabled ? 'stop-feed' : 'start-feed';

            // Per-camera detection indicators
            const camDet = (this._cameraDetectionSettings || {})[String(camera.id)] || {};
            const defDet = this._defaultDetection || detection_defaults();
            const motionOn = camDet.motion ?? defDet.motion ?? true;
            const objectsOn = camDet.objects ?? defDet.objects ?? true;
            const facesOn = camDet.faces ?? defDet.faces ?? true;

            return `
                <article class="camera-card" data-camera-card="${camera.id}" draggable="true" data-drag-id="${camera.id}">
                    <div class="camera-card-header">
                        <div style="display:flex;align-items:center;gap:10px;">
                            <span class="drag-handle" title="Drag to reorder">
                                <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor"><circle cx="4" cy="2" r="1.5"/><circle cx="10" cy="2" r="1.5"/><circle cx="4" cy="7" r="1.5"/><circle cx="10" cy="7" r="1.5"/><circle cx="4" cy="12" r="1.5"/><circle cx="10" cy="12" r="1.5"/></svg>
                            </span>
                            <div class="camera-meta">
                                <strong>${escapeHtml(camera.name)}</strong>
                                <span>${escapeHtml(camera.host)}:${escapeHtml(camera.port)}${escapeHtml(camera.path)}</span>
                            </div>
                        </div>
                        <div style="display:flex;align-items:center;gap:6px;">
                            <div class="det-indicators" title="Active detections">
                                ${motionOn ? '<span class="det-ind on">🏃</span>' : '<span class="det-ind off">🏃</span>'}
                                ${objectsOn ? '<span class="det-ind on">🚗</span>' : '<span class="det-ind off">🚗</span>'}
                                ${facesOn ? '<span class="det-ind on">😶</span>' : '<span class="det-ind off">😶</span>'}
                            </div>
                            ${enabled ? `<button class="btn-ghost focus-btn" type="button" onclick="window._openCameraFocus(${camera.id},'${escapeHtml(camera.name).replace(/'/g, "\\'")}')" title="Enlarge">
                                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M1 5V1h4M11 1h4v4M15 11v4h-4M5 15H1v-4"/></svg>
                            </button>` : ''}
                            <span class="status-badge ${this.getStatusClass(state.status)}" data-role="status-badge">${escapeHtml(state.status)}</span>
                        </div>
                    </div>
                    ${enabled
                        ? `<img class="feed-frame" src="/api/stream/${camera.id}" alt="Live stream for ${escapeHtml(camera.name)}" onclick="window._openCameraFocus(${camera.id},'${escapeHtml(camera.name).replace(/'/g, "\\'")}')" style="cursor:pointer;" title="Click to enlarge">`
                        : `<div class="feed-placeholder">Stream is stopped for ${escapeHtml(camera.name)}.<br>Use Start or Connect to resume.</div>`}
                    <div class="camera-card-footer">
                        <div class="camera-meta">
                            <strong data-role="frame-count">${state.frameCount.toLocaleString()} frames</strong>
                            <span data-role="recording-label">${state.recording ? '● REC' : 'Idle'}</span>
                        </div>
                        <div class="camera-actions">
                            <button class="${state.recording ? 'icon-btn recording' : 'icon-btn'}" type="button" data-action="toggle-recording" data-id="${camera.id}" data-role="record-button" title="${state.recording ? 'Stop recording' : 'Start recording'}">
                                <svg width="16" height="16" viewBox="0 0 16 16" fill="${state.recording ? '#ef4444' : 'currentColor'}"><circle cx="8" cy="8" r="6"/></svg>
                            </button>
                            <button class="icon-btn" type="button" data-action="snapshot-camera" data-id="${camera.id}" title="Take snapshot">
                                <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="1" y="3" width="14" height="11" rx="2"/><circle cx="8" cy="9" r="3"/><path d="M5 3l1-2h4l1 2"/></svg>
                            </button>
                            <button class="icon-btn" type="button" data-action="${streamAction}" data-id="${camera.id}" data-role="stream-button" title="${enabled ? 'Stop stream' : 'Start stream'}">
                                ${enabled
                                    ? '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="10" height="10" rx="1"/></svg>'
                                    : '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M4 2l10 6-10 6V2z"/></svg>'}
                            </button>
                        </div>
                    </div>
                </article>
            `;
        }).join('');

        container.innerHTML = pagHeader + cards;

        // Setup drag-and-drop
        this._setupDragAndDrop(container);
    }

    _setupDragAndDrop(container) {
        let dragId = null;
        const cards = container.querySelectorAll('[draggable="true"]');
        cards.forEach(card => {
            card.addEventListener('dragstart', (e) => {
                dragId = parseInt(card.dataset.dragId);
                card.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
            });
            card.addEventListener('dragend', () => {
                card.classList.remove('dragging');
                dragId = null;
                container.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
            });
            card.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                card.classList.add('drag-over');
            });
            card.addEventListener('dragleave', () => {
                card.classList.remove('drag-over');
            });
            card.addEventListener('drop', (e) => {
                e.preventDefault();
                card.classList.remove('drag-over');
                const dropId = parseInt(card.dataset.dragId);
                if (dragId === null || dragId === dropId) return;
                // Reorder cameras array
                const fromIdx = this.cameras.findIndex(c => c.id === dragId);
                const toIdx = this.cameras.findIndex(c => c.id === dropId);
                if (fromIdx === -1 || toIdx === -1) return;
                const [moved] = this.cameras.splice(fromIdx, 1);
                this.cameras.splice(toIdx, 0, moved);
                // Save order
                this.cameraOrder = this.cameras.map(c => c.id);
                localStorage.setItem('nvr-camera-order', JSON.stringify(this.cameraOrder));
                this.renderCameraGrid();
            });
        });
    }

    renderCameraList() {
        const container = document.getElementById('camera-list');
        if (!container) return;

        if (!this.cameras.length) {
            container.innerHTML = '<div class="empty-inline">No cameras available.</div>';
            return;
        }

        container.innerHTML = this.cameras.map((camera) => {
            const state = this.getCameraState(camera);
            const enabled = this.feedState.get(camera.id) !== false;

            const camType = camera.type || 'rtsp';
            const typeBadge = camType === 'mjpeg'
                ? '<span style="background:#e67e22;color:#fff;font-size:.65rem;padding:1px 5px;border-radius:3px;margin-left:6px;">MJPEG</span>'
                : '<span style="background:#2980b9;color:#fff;font-size:.65rem;padding:1px 5px;border-radius:3px;margin-left:6px;">RTSP</span>';
            const hostInfo = camType === 'mjpeg'
                ? escapeHtml(camera.stream_url || `${camera.host}:${camera.port}`)
                : `${escapeHtml(camera.host)}:${escapeHtml(camera.port)}${escapeHtml(camera.path)}`;

            const camDetection = this._cameraDetectionSettings || {};
            const camDet = camDetection[String(camera.id)] || {};
            const defDet = this._defaultDetection || detection_defaults();
            const motionOn = camDet.motion ?? defDet.motion ?? true;
            const objectsOn = camDet.objects ?? defDet.objects ?? true;
            const facesOn = camDet.faces ?? defDet.faces ?? true;
            const isCustom = String(camera.id) in camDetection;

            return `
                <div class="list-card" data-camera-list-item="${camera.id}" style="position:relative;">
                    <div class="list-card-row">
                        <div class="list-card-meta">
                            <strong>${escapeHtml(camera.name)}${typeBadge}</strong>
                            <small>${hostInfo}</small>
                        </div>
                        <span class="status-badge ${this.getStatusClass(state.status)}" data-role="status-badge">${escapeHtml(state.status)}</span>
                    </div>
                    <div class="action-row">
                        <button class="btn" type="button" data-action="connect-camera" data-id="${camera.id}" data-role="connect-button" ${enabled ? 'disabled' : ''}>Connect</button>
                        <button class="btn-secondary" type="button" data-action="disconnect-camera" data-id="${camera.id}" data-role="disconnect-button" ${enabled ? '' : 'disabled'}>Disconnect</button>
                    </div>
                    <div class="cam-detection-row">
                        <label class="cam-det-toggle" title="Motion detection">
                            <input type="checkbox" ${motionOn ? 'checked' : ''} onchange="window._nvrApp.saveCameraDetection(${camera.id},'motion',this.checked)">
                            <span>🏃</span>
                        </label>
                        <label class="cam-det-toggle" title="Object detection">
                            <input type="checkbox" ${objectsOn ? 'checked' : ''} onchange="window._nvrApp.saveCameraDetection(${camera.id},'objects',this.checked)">
                            <span>🚗</span>
                        </label>
                        <label class="cam-det-toggle" title="Face detection">
                            <input type="checkbox" ${facesOn ? 'checked' : ''} onchange="window._nvrApp.saveCameraDetection(${camera.id},'faces',this.checked)">
                            <span>😶</span>
                        </label>
                        ${isCustom ? `<button class="btn-ghost" style="font-size:.65rem;padding:1px 4px;opacity:.5;" onclick="window._nvrApp.resetCameraDetection(${camera.id})" title="Reset to defaults">↺</button>` : ''}
                    </div>
                    <div style="position:absolute;bottom:8px;right:8px;display:flex;gap:6px;">
                        <button class="btn-ghost" type="button" data-action="edit-camera" data-id="${camera.id}" title="Edit" style="font-size:14px;padding:2px 6px;opacity:.6;">&#9998;</button>
                        <button class="btn-ghost" type="button" data-action="delete-camera" data-id="${camera.id}" title="Delete" style="font-size:14px;padding:2px 6px;opacity:.6;color:#e74c3c;">&#128465;</button>
                    </div>
                </div>
            `;
        }).join('');
    }

    renderRecordings(files) {
        const container = document.getElementById('recordings-list');
        if (!container) return;

        if (!files.length) {
            container.innerHTML = '<div class="empty-inline">No recordings found.</div>';
            return;
        }

        container.innerHTML = files.map((file) => `
            <div class="list-card">
                <div class="list-card-row">
                    <div class="list-card-meta">
                        <strong>${escapeHtml(file.name)}</strong>
                        <small>${escapeHtml(file.modified)} · ${escapeHtml(file.size_mb != null ? `${file.size_mb} MB` : this.formatFileSize(file.size))}</small>
                    </div>
                    <a class="btn" href="/api/recordings/${encodeURIComponent(file.name)}">Download</a>
                </div>
            </div>
        `).join('');
    }

    renderSDCardFiles(data) {
        const container = document.getElementById('sdcard-files');
        if (!container) return;

        if (!data || !data.files?.length) {
            container.innerHTML = '<div class="empty-inline">No SD card files available for this camera.</div>';
            return;
        }

        const camera = this.cameras.find((item) => item.name === data.camera || item.host === data.host);
        const cameraId = camera?.id || document.getElementById('sdcard-camera-select')?.value;

        container.innerHTML = `
            <div class="list-card">
                <div class="list-card-meta">
                    <strong>${escapeHtml(data.camera)}</strong>
                    <small>${escapeHtml(data.host)} · ${data.files.length} file(s)</small>
                </div>
            </div>
            <div class="table-wrap">
                <table>
                    <thead>
                        <tr>
                            <th>Name</th>
                            <th>Path</th>
                            <th>Size</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${data.files.map((file) => `
                            <tr>
                                <td>${escapeHtml(file.name)}</td>
                                <td>${escapeHtml(file.path)}</td>
                                <td>${escapeHtml(this.formatFileSize(file.size))}</td>
                                <td><button class="btn-secondary" type="button" data-action="download-sd-file" data-id="${cameraId}" data-path="${escapeHtml(file.path)}">Download</button></td>
                            </tr>
                        `).join('')}
                    </tbody>
                </table>
            </div>
        `;
    }

    renderEvents(events) {
        const container = document.getElementById('events-table-wrap');
        if (!container) return;

        if (!events.length) {
            container.innerHTML = '<div class="empty-inline">No events match the current filter.</div>';
            return;
        }

        // Show summary counts by type with a link to the gallery
        const typeCounts = {};
        events.forEach(e => { typeCounts[e.detection_type] = (typeCounts[e.detection_type] || 0) + 1; });
        const typeColors = { motion: '#f39c12', person: '#2ecc71', vehicle: '#3498db', face: '#e74c3c', animal: '#9b59b6', object: '#1abc9c' };

        const badges = Object.entries(typeCounts).map(([type, count]) => {
            const color = typeColors[type] || '#888';
            return `<span class="event-summary-badge" style="background:${color}">${escapeHtml(type)} <strong>${count}</strong></span>`;
        }).join('');

        container.innerHTML = `
            <div class="events-summary">
                <div class="events-summary-total">${events.length} recent events</div>
                <div class="events-summary-badges">${badges}</div>
                <a href="/events" target="_blank" class="btn-primary events-gallery-btn">View Events Gallery ↗</a>
            </div>`;
    }

    renderStatus(status) {
        this.status = status;

        const activeEl = document.getElementById('header-active-cameras');
        const recordingEl = document.getElementById('header-recording-count');
        if (activeEl) activeEl.textContent = String(status.streams_active ?? 0);
        if (recordingEl) recordingEl.textContent = String(status.recordings_count ?? 0);

        this.syncCameraStatusUI();
    }

    renderScanResults(cameras) {
        const container = document.getElementById('scan-results');
        if (!container) return;

        if (!cameras.length) {
            container.innerHTML = '<div class="empty-inline">No cameras discovered on the network.</div>';
            return;
        }

        container.innerHTML = cameras.map((camera) => {
            const existing = this.cameras.find(c => c.host === camera.host);
            const badge = existing
                ? '<span style="font-size:11px;background:#2d6a4f;color:#b7e4c7;padding:2px 8px;border-radius:10px;">Registered</span>'
                : '<span style="font-size:11px;background:#7c5cfc;color:#fff;padding:2px 8px;border-radius:10px;">New</span>';

            const camType = camera.type || 'rtsp';
            const typeBadge = camType === 'mjpeg'
                ? '<span style="font-size:10px;background:#e67e22;color:#fff;padding:1px 6px;border-radius:8px;margin-left:4px;">MJPEG</span>'
                : '<span style="font-size:10px;background:#2980b9;color:#fff;padding:1px 6px;border-radius:8px;margin-left:4px;">RTSP</span>';

            const hostInfo = camType === 'mjpeg'
                ? escapeHtml(camera.stream_url || `${camera.host}:${camera.port}`)
                : `${escapeHtml(camera.host)}:${escapeHtml(camera.port ?? 554)}${escapeHtml(camera.path || '/onvif1')}`;

            return `
            <div class="list-card">
                <div class="list-card-row">
                    <div class="list-card-meta">
                        <strong>${escapeHtml(camera.name || camera.host)}</strong> ${typeBadge} ${badge}
                        <small>${hostInfo}</small>
                        ${camera.server ? `<small style="color:#888;">Server: ${escapeHtml(camera.server)}</small>` : ''}
                    </div>
                    <button
                        class="btn"
                        type="button"
                        data-action="add-scanned-camera"
                        data-name="${escapeHtml(camera.name || camera.host)}"
                        data-host="${escapeHtml(camera.host)}"
                        data-port="${escapeHtml(camera.port ?? 554)}"
                        data-path="${escapeHtml(camera.path || '/onvif1')}"
                        data-type="${escapeHtml(camType)}"
                        data-stream-url="${escapeHtml(camera.stream_url || '')}"
                        ${existing ? 'disabled style="opacity:.5"' : ''}
                    >
                        ${existing ? 'Added' : 'Add'}
                    </button>
                </div>
            </div>
        `}).join('');
    }

    // --- Event Listeners ---
    setupEventListeners() {
        document.querySelectorAll('[data-tab]').forEach((button) => {
            button.addEventListener('click', () => this.switchTab(button.dataset.tab));
        });

        document.getElementById('sidebar-toggle')?.addEventListener('click', () => {
            document.body.classList.toggle('sidebar-open');
        });

        document.getElementById('scan-network-btn')?.addEventListener('click', () => this.scanNetwork());
        document.getElementById('scan-back-btn')?.addEventListener('click', () => this.showCamerasDefaultView());
        document.getElementById('refresh-recordings-btn')?.addEventListener('click', () => this.loadRecordings());
        document.getElementById('sdcard-list-btn')?.addEventListener('click', () => {
            const cameraId = document.getElementById('sdcard-camera-select')?.value;
            this.loadSDCardFiles(cameraId);
        });

        document.getElementById('event-type-filter')?.addEventListener('change', (event) => {
            this.loadEvents(event.target.value);
        });

        // Camera type toggle (RTSP vs MJPEG)
        document.getElementById('camera-type')?.addEventListener('change', (e) => {
            const isMjpeg = e.target.value === 'mjpeg';
            document.querySelectorAll('.rtsp-field').forEach(el => el.style.display = isMjpeg ? 'none' : '');
            document.querySelectorAll('.mjpeg-field').forEach(el => el.style.display = isMjpeg ? '' : 'none');
            // Adjust required attributes
            document.getElementById('camera-host').required = !isMjpeg;
            document.getElementById('camera-port').required = !isMjpeg;
            document.getElementById('camera-path').required = !isMjpeg;
            const urlInput = document.getElementById('camera-stream-url');
            if (urlInput) urlInput.required = isMjpeg;
        });

        document.getElementById('add-camera-form')?.addEventListener('submit', async (event) => {
            event.preventDefault();
            const form = new FormData(event.target);
            const camType = form.get('type')?.toString() || 'rtsp';

            if (camType === 'mjpeg') {
                const streamUrl = form.get('stream_url')?.toString().trim() || '';
                // Extract host from URL for DB uniqueness
                let host = '0.0.0.0';
                try { host = new URL(streamUrl).hostname; } catch {}
                let port = 8081;
                try { port = parseInt(new URL(streamUrl).port) || 8081; } catch {}
                await this.addCamera({
                    name: form.get('name')?.toString().trim(),
                    host: host,
                    port: port,
                    path: '',
                    username: '',
                    password: '',
                    type: 'mjpeg',
                    stream_url: streamUrl,
                });
            } else {
                await this.addCamera({
                    name: form.get('name')?.toString().trim(),
                    host: form.get('host')?.toString().trim(),
                    port: Number(form.get('port') || 554),
                    path: form.get('path')?.toString().trim() || '/onvif1',
                    username: form.get('username')?.toString().trim() || 'admin',
                    password: form.get('password')?.toString() || '',
                    type: 'rtsp',
                });
            }
            event.target.reset();
            document.getElementById('camera-type').dispatchEvent(new Event('change'));
            const pathInput = document.getElementById('camera-path');
            const portInput = document.getElementById('camera-port');
            const usernameInput = document.getElementById('camera-username');
            if (pathInput) pathInput.value = '/onvif1';
            if (portInput) portInput.value = '554';
            if (usernameInput) usernameInput.value = 'admin';
        });

        document.addEventListener('click', async (event) => {
            const target = event.target.closest('[data-action]');
            if (!target) return;

            const id = Number(target.dataset.id);
            const action = target.dataset.action;

            if (action === 'connect-camera' || action === 'start-feed') {
                await this.startStream(id);
            } else if (action === 'disconnect-camera' || action === 'stop-feed') {
                await this.stopStream(id);
            } else if (action === 'toggle-recording') {
                await this.toggleRecording(id);
            } else if (action === 'snapshot-camera') {
                await this.takeSnapshot(id);
            } else if (action === 'download-sd-file') {
                await this.downloadSDFile(id, target.dataset.path);
            } else if (action === 'add-scanned-camera') {
                await this.addCamera({
                    name: target.dataset.name,
                    host: target.dataset.host,
                    port: Number(target.dataset.port || 554),
                    path: target.dataset.path || '/onvif1',
                    username: 'admin',
                    password: '',
                    type: target.dataset.type || 'rtsp',
                    stream_url: target.dataset.streamUrl || '',
                });
                // Update button to show it's been added
                target.textContent = 'Added';
                target.disabled = true;
                target.style.opacity = '.5';
            } else if (action === 'cam-page-prev') {
                this.cameraPage = Math.max(0, this.cameraPage - 1);
                this.renderCameraGrid();
            } else if (action === 'cam-page-next') {
                this.cameraPage++;
                this.renderCameraGrid();
            } else if (action === 'delete-camera') {
                const camera = this.cameras.find(c => c.id === id);
                if (confirm(`Delete camera "${camera?.name || id}"? This cannot be undone.`)) {
                    try {
                        await this.api('DELETE', `/api/cameras/${id}`);
                        this.showToast('Camera deleted', 'success');
                        await this.loadCameras();
                    } catch (e) {
                        this.showToast(`Delete failed: ${e.message}`, 'error');
                    }
                }
            } else if (action === 'edit-camera') {
                this.openEditCameraDialog(id);
            } else if (action === 'save-edit-camera') {
                await this.saveEditCamera();
            } else if (action === 'cancel-edit-camera') {
                document.getElementById('edit-camera-dialog').style.display = 'none';
            } else if (action === 'save-storage-dir') {
                await this.saveStorageDir();
            }
        });
    }

    switchTab(tab) {
        this.activeTab = tab;
        document.querySelectorAll('[data-tab]').forEach((button) => {
            button.classList.toggle('active', button.dataset.tab === tab);
        });
        document.querySelectorAll('.tab-panel').forEach((panel) => {
            panel.classList.toggle('active', panel.id === `tab-${tab}`);
        });

        if (tab === 'recordings') {
            this.loadRecordings();
        } else if (tab === 'events') {
            this.loadEvents();
            this.loadStorageSettings();
        }

        if (window.innerWidth <= 960) {
            document.body.classList.remove('sidebar-open');
        }
    }

    // --- Polling ---
    startStatusPolling() {
        if (this.statusInterval) clearInterval(this.statusInterval);
        this.statusInterval = setInterval(() => this.loadStatus(), 10000);
    }

    startEventsPolling() {
        if (this.eventsInterval) clearInterval(this.eventsInterval);
        this.eventsInterval = setInterval(() => this.loadEvents(), 30000);
    }

    // --- Detection Settings ---
    async loadDetectionSettings() {
        try {
            const data = await this.api('GET', '/api/detection');
            const settings = data.default || data;
            this._cameraDetectionSettings = data.cameras || {};
            this._defaultDetection = settings;
        } catch (e) {
            console.error('Failed to load detection settings', e);
        }
    }

    async saveCameraDetection(cameraId, type, enabled) {
        try {
            await this.api('POST', `/api/detection/${cameraId}`, { [type]: enabled });
            this.showToast(`Camera ${cameraId} ${type} ${enabled ? 'enabled' : 'disabled'}`, 'info');
        } catch (e) {
            this.showToast(`Failed to update: ${e.message}`, 'error');
        }
    }

    async resetCameraDetection(cameraId) {
        try {
            await this.api('DELETE', `/api/detection/${cameraId}`);
            this.showToast(`Camera ${cameraId} reset to defaults`, 'info');
            this.loadDetectionSettings();
            this.loadCameras();
        } catch (e) {
            this.showToast(`Failed to reset: ${e.message}`, 'error');
        }
    }

    // --- Storage Settings ---
    async loadStorageSettings() {
        try {
            const data = await this.api('GET', '/api/settings/storage');
            const input = document.getElementById('storage-dir-input');
            const info = document.getElementById('storage-info');
            if (input) input.value = data.storage_dir || '';
            if (info) {
                info.innerHTML = `${data.storage_dir}<br>├── recordings/<br>│   └── {camera}/{YYYY}/{MM}/{DD}/<br>├── snapshots/<br>│   └── {camera}/{YYYY}/{MM}/{DD}/<br>└── clips/<br>    └── {camera}/{YYYY}/{MM}/{DD}/`;
            }
        } catch (e) {
            console.error('Failed to load storage settings', e);
        }
    }

    async saveStorageDir() {
        const input = document.getElementById('storage-dir-input');
        const statusEl = document.getElementById('storage-dir-status');
        const dir = input?.value?.trim();
        if (!dir) {
            if (statusEl) { statusEl.textContent = '⚠ Please enter a directory path'; statusEl.style.color = '#f59e0b'; }
            return;
        }
        try {
            const result = await this.api('POST', '/api/settings/storage', { storage_dir: dir });
            if (statusEl) {
                statusEl.textContent = '✓ Storage directory updated';
                statusEl.style.color = '#10b981';
                setTimeout(() => { statusEl.textContent = ''; }, 3000);
            }
            this.loadStorageSettings();
        } catch (e) {
            if (statusEl) {
                statusEl.textContent = `✗ ${e.message}`;
                statusEl.style.color = '#ef4444';
            }
        }
    }

    // --- Helpers ---
    showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        if (!container) return;

        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.textContent = message;
        container.appendChild(toast);

        window.setTimeout(() => {
            toast.remove();
        }, 3500);
    }

    formatFileSize(bytes) {
        const value = Number(bytes ?? 0);
        if (!Number.isFinite(value) || value <= 0) return '0 B';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
        const amount = value / (1024 ** index);
        return `${amount.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
    }

    formatTime(iso) {
        if (!iso) return '—';
        // DB stores UTC via datetime('now') — append Z so JS parses as UTC
        let s = String(iso);
        if (!s.endsWith('Z') && !s.includes('+')) s = s.replace(' ', 'T') + 'Z';
        const date = new Date(s);
        if (Number.isNaN(date.getTime())) return String(iso);
        return date.toLocaleString();
    }

    populateCameraSelect() {
        const select = document.getElementById('sdcard-camera-select');
        if (!select) return;

        const current = select.value;
        const options = ['<option value="">Select camera</option>']
            .concat(this.cameras.map((camera) => `<option value="${camera.id}">${escapeHtml(camera.name)} (${escapeHtml(camera.host)})</option>`));
        select.innerHTML = options.join('');

        if (this.cameras.some((camera) => String(camera.id) === current)) {
            select.value = current;
        }
    }

    getCameraState(camera) {
        const stream = camera ? this.status?.streams?.[String(camera.id)] || {} : {};
        return {
            status: stream.status || camera?.stream_status || 'stopped',
            frameCount: Number(stream.frame_count ?? camera?.frame_count ?? 0),
            recording: Boolean(stream.recording),
        };
    }

    getStatusClass(status) {
        return `status-${String(status || 'stopped').toLowerCase()}`;
    }

    updateConnectionState(isOnline, label) {
        const dot = document.getElementById('connection-dot');
        const text = document.getElementById('footer-connection-status');
        if (dot) {
            dot.classList.toggle('online', isOnline);
            dot.classList.toggle('offline', !isOnline);
        }
        if (text) {
            text.textContent = label;
        }
    }

    updateRefreshTime() {
        const el = document.getElementById('last-refresh-time');
        if (el) {
            el.textContent = new Date().toLocaleTimeString();
        }
    }

    syncCameraStatusUI() {
        this.cameras.forEach((camera) => {
            const state = this.getCameraState(camera);
            const enabled = this.feedState.get(camera.id) !== false;

            const card = document.querySelector(`[data-camera-card="${camera.id}"]`);
            if (card) {
                const badge = card.querySelector('[data-role="status-badge"]');
                const frameCount = card.querySelector('[data-role="frame-count"]');
                const recordingLabel = card.querySelector('[data-role="recording-label"]');
                const recordButton = card.querySelector('[data-role="record-button"]');
                const streamButton = card.querySelector('[data-role="stream-button"]');

                if (badge) {
                    badge.className = `status-badge ${this.getStatusClass(state.status)}`;
                    badge.textContent = state.status;
                }
                if (frameCount) frameCount.textContent = `${state.frameCount.toLocaleString()} frames`;
                if (recordingLabel) recordingLabel.textContent = state.recording ? '● REC' : 'Idle';
                if (recordButton) {
                    recordButton.innerHTML = `<svg width="16" height="16" viewBox="0 0 16 16" fill="${state.recording ? '#ef4444' : 'currentColor'}"><circle cx="8" cy="8" r="6"/></svg>`;
                    recordButton.className = state.recording ? 'icon-btn recording' : 'icon-btn';
                    recordButton.title = state.recording ? 'Stop recording' : 'Start recording';
                }
                if (streamButton) {
                    streamButton.dataset.action = enabled ? 'stop-feed' : 'start-feed';
                    streamButton.innerHTML = enabled
                        ? '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><rect x="3" y="3" width="10" height="10" rx="1"/></svg>'
                        : '<svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M4 2l10 6-10 6V2z"/></svg>';
                    streamButton.title = enabled ? 'Stop stream' : 'Start stream';
                }
            }

            const listItem = document.querySelector(`[data-camera-list-item="${camera.id}"]`);
            if (listItem) {
                const badge = listItem.querySelector('[data-role="status-badge"]');
                const connectButton = listItem.querySelector('[data-role="connect-button"]');
                const disconnectButton = listItem.querySelector('[data-role="disconnect-button"]');

                if (badge) {
                    badge.className = `status-badge ${this.getStatusClass(state.status)}`;
                    badge.textContent = state.status;
                }
                if (connectButton) connectButton.disabled = enabled;
                if (disconnectButton) disconnectButton.disabled = !enabled;
            }
        });
    }

    openEditCameraDialog(cameraId) {
        const camera = this.cameras.find(c => c.id === cameraId);
        if (!camera) return;

        let dialog = document.getElementById('edit-camera-dialog');
        if (!dialog) {
            dialog = document.createElement('div');
            dialog.id = 'edit-camera-dialog';
            document.body.appendChild(dialog);
        }

        dialog.style.cssText = 'display:flex;position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,.7);align-items:center;justify-content:center;';
        dialog.innerHTML = `
            <div style="background:#1e1e2e;border-radius:12px;padding:24px;width:400px;max-width:90vw;box-shadow:0 8px 32px rgba(0,0,0,.5);">
                <h3 style="margin:0 0 16px;color:#e0e0e0;">Edit Camera</h3>
                <div style="display:flex;flex-direction:column;gap:10px;">
                    <label style="color:#aaa;font-size:13px;">Name
                        <input id="edit-cam-name" type="text" value="${escapeHtml(camera.name)}" style="width:100%;padding:8px;border-radius:6px;border:1px solid #444;background:#2a2a3e;color:#fff;margin-top:4px;">
                    </label>
                    <label style="color:#aaa;font-size:13px;">Host / IP
                        <input id="edit-cam-host" type="text" value="${escapeHtml(camera.host)}" style="width:100%;padding:8px;border-radius:6px;border:1px solid #444;background:#2a2a3e;color:#fff;margin-top:4px;">
                    </label>
                    <label style="color:#aaa;font-size:13px;">Port
                        <input id="edit-cam-port" type="number" value="${camera.port}" style="width:100%;padding:8px;border-radius:6px;border:1px solid #444;background:#2a2a3e;color:#fff;margin-top:4px;">
                    </label>
                    <label style="color:#aaa;font-size:13px;">RTSP Path
                        <input id="edit-cam-path" type="text" value="${escapeHtml(camera.path)}" style="width:100%;padding:8px;border-radius:6px;border:1px solid #444;background:#2a2a3e;color:#fff;margin-top:4px;">
                    </label>
                    <label style="color:#aaa;font-size:13px;">Password <small>(leave empty to keep current)</small>
                        <input id="edit-cam-password" type="password" placeholder="unchanged" style="width:100%;padding:8px;border-radius:6px;border:1px solid #444;background:#2a2a3e;color:#fff;margin-top:4px;">
                    </label>
                </div>
                <input type="hidden" id="edit-cam-id" value="${camera.id}">
                <div style="display:flex;gap:10px;margin-top:18px;justify-content:flex-end;">
                    <button class="btn-secondary" type="button" data-action="cancel-edit-camera">Cancel</button>
                    <button class="btn" type="button" data-action="save-edit-camera">Save</button>
                </div>
            </div>
        `;
    }

    async saveEditCamera() {
        const id = Number(document.getElementById('edit-cam-id').value);
        const body = {
            name: document.getElementById('edit-cam-name').value.trim(),
            host: document.getElementById('edit-cam-host').value.trim(),
            port: Number(document.getElementById('edit-cam-port').value),
            path: document.getElementById('edit-cam-path').value.trim(),
        };
        const password = document.getElementById('edit-cam-password').value;
        if (password) body.password = password;

        try {
            await this.api('PUT', `/api/cameras/${id}`, body);
            document.getElementById('edit-camera-dialog').style.display = 'none';
            this.showToast('Camera updated', 'success');
            await this.loadCameras();
        } catch (e) {
            this.showToast(`Update failed: ${e.message}`, 'error');
        }
    }
}

// Initialize on DOM ready
function detection_defaults() { return { motion: true, objects: true, faces: true }; }
document.addEventListener('DOMContentLoaded', () => { window.app = new NVRApp(); window._nvrApp = window.app; });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') window._closeLightbox(); });
