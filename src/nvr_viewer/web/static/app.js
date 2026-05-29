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
        this.setupEventListeners();
        await Promise.all([
            this.loadCameras(),
            this.loadRecordings(),
            this.loadEvents(),
            this.loadStatus(),
            this.loadDetectionSettings(),
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

        container.innerHTML = this.cameras.map((camera) => {
            const state = this.getCameraState(camera);
            const enabled = this.feedState.get(camera.id) !== false;
            const streamAction = enabled ? 'stop-feed' : 'start-feed';
            const streamLabel = enabled ? 'Stop' : 'Start';
            const recordLabel = state.recording ? '● Recording' : 'Record';

            return `
                <article class="camera-card" data-camera-card="${camera.id}">
                    <div class="camera-card-header">
                        <div class="camera-meta">
                            <strong>${escapeHtml(camera.name)}</strong>
                            <span>${escapeHtml(camera.host)}:${escapeHtml(camera.port)}${escapeHtml(camera.path)}</span>
                        </div>
                        <span class="status-badge ${this.getStatusClass(state.status)}" data-role="status-badge">${escapeHtml(state.status)}</span>
                    </div>
                    ${enabled
                        ? `<img class="feed-frame" src="/api/stream/${camera.id}" alt="Live stream for ${escapeHtml(camera.name)}">`
                        : `<div class="feed-placeholder">Stream is stopped for ${escapeHtml(camera.name)}.<br>Use Start or Connect to resume.</div>`}
                    <div class="camera-card-footer">
                        <div class="camera-meta">
                            <strong data-role="frame-count">${state.frameCount.toLocaleString()} frames</strong>
                            <span data-role="recording-label">${state.recording ? 'Recording in progress' : 'Idle'}</span>
                        </div>
                        <div class="camera-actions">
                            <button class="${state.recording ? 'btn-danger btn-recording' : 'btn-secondary'}" type="button" data-action="toggle-recording" data-id="${camera.id}" data-role="record-button">${recordLabel}</button>
                            <button class="btn-ghost" type="button" data-action="snapshot-camera" data-id="${camera.id}">Snapshot</button>
                            <button class="btn" type="button" data-action="${streamAction}" data-id="${camera.id}" data-role="stream-button">${streamLabel}</button>
                        </div>
                    </div>
                </article>
            `;
        }).join('');
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

        // Detection toggle checkboxes
        ['det-motion', 'det-objects', 'det-faces'].forEach(id => {
            document.getElementById(id)?.addEventListener('change', () => this.saveDetectionSettings());
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
            const settings = await this.api('GET', '/api/detection');
            document.getElementById('det-motion').checked = !!settings.motion;
            document.getElementById('det-objects').checked = !!settings.objects;
            document.getElementById('det-faces').checked = !!settings.faces;
        } catch (e) {
            console.error('Failed to load detection settings', e);
        }
    }

    async saveDetectionSettings() {
        const body = {
            motion: document.getElementById('det-motion').checked,
            objects: document.getElementById('det-objects').checked,
            faces: document.getElementById('det-faces').checked,
        };
        const statusEl = document.getElementById('detection-status');
        try {
            await this.api('POST', '/api/detection', body);
            if (statusEl) {
                statusEl.textContent = '✓ Settings saved';
                statusEl.className = 'detection-status saved';
                setTimeout(() => { statusEl.textContent = ''; statusEl.className = 'detection-status'; }, 2000);
            }
        } catch (e) {
            this.showToast(`Failed to update detection: ${e.message}`, 'error');
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
        const date = new Date(iso);
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
                if (recordingLabel) recordingLabel.textContent = state.recording ? 'Recording in progress' : 'Idle';
                if (recordButton) {
                    recordButton.textContent = state.recording ? '● Recording' : 'Record';
                    recordButton.className = state.recording ? 'btn-danger btn-recording' : 'btn-secondary';
                }
                if (streamButton) {
                    streamButton.dataset.action = enabled ? 'stop-feed' : 'start-feed';
                    streamButton.textContent = enabled ? 'Stop' : 'Start';
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
document.addEventListener('DOMContentLoaded', () => { window.app = new NVRApp(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') window._closeLightbox(); });
