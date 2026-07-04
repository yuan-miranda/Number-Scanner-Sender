function otpPanel() {
    return {
        // ── constants ──────────────────────────────────────────────
        SIDES: ['left', 'right'],
        MODEL_OPTIONS: [
            'gemini-3.1-flash-lite',
            'gemini-3.5-flash',
            'gemini-2.5-flash',
            'gemini-2.5-pro'
        ],

        // ── state ──────────────────────────────────────────────────
        dark: false,
        configLoaded: false,
        servoCount: 0,
        tokens: [],
        angles: {},
        reTriggers: {},
        cameras: { left: 0, right: 1 },
        captureDelay: 1000,
        camOpen: { left: true, right: true },
        camSrc: { left: '', right: '' },
        latestSrc: '/captures/latest.jpg',
        captureMissing: false,
        capturePolling: true,

        // ── prompt editor state ────────────────────────────────────
        promptOpen: false,
        promptText: '',
        defaultPromptText: '',
        savedPromptText: '',
        promptPreviewMode: false,
        promptConfigMode: false,
        localModelsConfig: [],
        modelsConfigDirty: false,

        // ── init ───────────────────────────────────────────────────
        init() {
            const saved = localStorage.getItem('theme');
            this.dark = saved
                ? saved === 'dark'
                : window.matchMedia('(prefers-color-scheme: dark)').matches;

            this.$watch('dark', val =>
                localStorage.setItem('theme', val ? 'dark' : 'light')
            );

            this.loadConfig();

            setInterval(() => {
                if (this.capturePolling)
                    this.latestSrc = this.bustUrl('/captures/latest.jpg');
            }, 1000);
        },

        // ── network helpers ────────────────────────────────────────
        post(url, body) {
            return fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
        },

        bustUrl(path) {
            return `${path}?t=${Date.now()}`;
        },

        // ── validation ─────────────────────────────────────────────
        isAngleValid(v) { const n = Number(v); return Number.isInteger(n) && n >= 1 && n <= 180; },
        isIdValid(v) { const n = Number(v); return Number.isInteger(n) && n >= 0 && n <= 3; },
        isDelayValid(v) { const n = Number(v); return Number.isInteger(n) && n >= 1000 && n <= 5000; },

        isNameValid(v) {
            const s = v.trim();
            return s === '' || /^[A-Za-z ]{1,12}$/.test(s);
        },

        isAliasListValid(v) {
            const s = v.trim();
            if (s === '') return true;
            const parts = s.split(',');
            if (parts.some(p => p.trim() === '')) return false;
            return parts.every(p => /^[A-Za-z]{1,24}$/.test(p.trim()));
        },

        parseAliases(v) {
            const seen = new Set();
            v.split(',').map(a => a.trim()).filter(Boolean).forEach(a => seen.add(a));
            return Array.from(seen);
        },

        // ── config load ────────────────────────────────────────────
        async loadConfig() {
            let data;
            try {
                data = await fetch('/get_config').then(r => r.json());
            } catch {
                this.configLoaded = 'error';
                return;
            }

            this.config = data;

            this.angles = { ...data.angles };
            this.reTriggers = { ...(data.re_trigger || {}) };
            this.captureDelay = data.capture_delay_ms ?? 1000;
            this.cameras = { ...data.cameras };
            this.servoCount = data.servo_count ?? Object.keys(data.angles).length;

            const meta = data.servo_meta || {};
            this.tokens = Array.from({ length: this.servoCount }, (_, i) => {
                const id = i + 1;
                const m = meta[String(id)] || {};
                const name = (m.name || '').trim();
                const aliases = Array.isArray(m.aliases) ? m.aliases : [];
                return { id, displayName: name, aliasString: aliases.join(', ') };
            });

            this.configLoaded = true;
            this.SIDES.forEach(side => this.openCamera(side, this.cameras[side]));
            await this.loadPrompt();

            // Load models arrangement state or instantiate baseline options
            if (data.models_config) {
                this.localModelsConfig = JSON.parse(JSON.stringify(data.models_config));
            } else {
                this.localModelsConfig = [
                    { model: 'gemini-3.1-flash-lite', priority: true },
                    { model: 'gemini-3.5-flash', priority: true }
                ];
                this.config.models_config = JSON.parse(JSON.stringify(this.localModelsConfig));
            }
            this.checkModelsDirty();
            this.modelsConfigDirty = false;
        },

        servoLabel(token) {
            return token.displayName || `Servo ${token.id}`;
        },

        // ── angle ──────────────────────────────────────────────────
        onAngleInput(e) {
            const ok = this.isAngleValid(e.target.value);
            e.target.classList.toggle('invalid', !ok);
        },

        saveAngleOnBlur(e, id) {
            const input = e.target;
            const v = Number(input.value);
            if (!this.isAngleValid(v)) { input.classList.add('invalid'); return false; }
            input.classList.remove('invalid');
            this.angles[id] = v;
            this.post('/set_angle', { servo: id, angle: v });
            return true;
        },

        testServo(el, id) {
            const input = el.closest('.servo-controls').querySelector('input[type=number]');
            const v = Number(input.value);
            if (this.isAngleValid(v))
                fetch(`/fire_servo?servo=${id}&angle=${v}`);
        },

        // ── name ───────────────────────────────────────────────────
        onNameInput(e) {
            const ok = this.isNameValid(e.target.value);
            e.target.classList.toggle('invalid', !ok);
        },

        saveName(el, token) {
            const input = el.closest('.servo-controls').querySelector('input.name-input');
            if (!this.isNameValid(input.value)) {
                input.classList.add('invalid');
                alert('Name must be letters only, up to 12 characters (or blank to clear it)');
                return false;
            }
            input.classList.remove('invalid');
            const name = input.value.trim();
            token.displayName = name;
            this.post('/set_servo_meta', { servo: token.id, name });
            input.value = name;
            return true;
        },

        // ── aliases ────────────────────────────────────────────────
        onAliasInput(e) {
            const ok = this.isAliasListValid(e.target.value);
            e.target.classList.toggle('invalid', !ok);
        },

        saveAliases(el, token) {
            const input = el.closest('.servo-controls').querySelector('input.alias-input');
            if (!this.isAliasListValid(input.value)) {
                input.classList.add('invalid');
                alert('Aliases must be comma-separated, letters only, up to 24 characters each (no blank entries)');
                return false;
            }
            input.classList.remove('invalid');
            const aliases = this.parseAliases(input.value);
            token.aliasString = aliases.join(', ');
            this.post('/set_servo_meta', { servo: token.id, aliases });
            input.value = token.aliasString;
            return true;
        },

        // ── re-trigger ─────────────────────────────────────────────
        onReTriggerChange(e, id) {
            this.reTriggers[id] = e.target.checked;
            this.post('/set_re_trigger', { servo: id, re_trigger: e.target.checked });
        },

        // ── camera ─────────────────────────────────────────────────
        camLabel(side) {
            const label = side.charAt(0).toUpperCase() + side.slice(1);
            return `Camera ${label} (ID: ${this.cameras[side] ?? '-'})`;
        },

        openCamera(side, camId) {
            this.camSrc[side] = this.bustUrl(`/video_feed/${camId}`);
            this.camOpen[side] = true;
        },

        setCamClosed(side) {
            this.camSrc[side] = '';
            this.camOpen[side] = false;
        },

        async toggleCamera(side) {
            if (this.camOpen[side]) {
                this.setCamClosed(side);
                const other = side === 'left' ? 'right' : 'left';
                const otherSameId = this.camOpen[other] && this.cameras[other] === this.cameras[side];
                if (!otherSameId)
                    await this.post('/release_camera', { cam_id: this.cameras[side] });
            } else {
                const res = await this.post('/set_camera', { side, cam_id: this.cameras[side] });
                if (res.ok) this.openCamera(side, this.cameras[side]);
            }
        },

        async saveCamera(el, side) {
            const input = el.closest('.cam-field').querySelector('input[type=number]');
            const v = Number(input.value);
            if (!this.isIdValid(v)) {
                input.classList.add('invalid');
                alert('Camera ID must be between 0 and 3');
                return;
            }
            input.classList.remove('invalid');
            this.cameras[side] = v;
            const res = await this.post('/set_camera', { side, cam_id: v });
            if (res.ok) this.openCamera(side, v);
        },

        onCamIdInput(e) {
            const ok = this.isIdValid(e.target.value);
            e.target.classList.toggle('invalid', !ok);
        },

        // ── accordion ──────────────────────────────────────────────
        onAccordionToggle(e) {
            if (e.target.open) {
                document.querySelectorAll('.servo-details').forEach(el => {
                    if (el !== e.target && el.open) el.open = false;
                });
            }
        },

        // ── delay ──────────────────────────────────────────────────
        onDelayInput(e) {
            const ok = this.isDelayValid(e.target.value);
            e.target.classList.toggle('invalid', !ok);
        },

        saveCaptureDelay(el) {
            const input = el.closest('.cam-field').querySelector('input[type=number]');
            const v = Number(input.value);
            if (!this.isDelayValid(v)) {
                input.classList.add('invalid');
                alert('Delay must be between 1000ms and 5000ms (1–5s)');
                return;
            }
            input.classList.remove('invalid');
            this.captureDelay = v;
            this.post('/set_capture_delay', { delay_ms: v });
        },

        reloadCapture() {
            this.capturePolling = true;
            this.latestSrc = this.bustUrl('/captures/latest.jpg');
        },

        // ── prompt editor ──────────────────────────────────────────
        async loadPrompt() {
            try {
                const data = await fetch('/get_prompt').then(r => r.json());
                this.defaultPromptText = data.default_template || '';
                this.promptText = data.prompt_template || this.defaultPromptText;
                this.savedPromptText = this.promptText;
            } catch {
                this.promptText = '';
                this.savedPromptText = '';
            }
        },

        togglePromptEditor() {
            this.promptOpen = !this.promptOpen;
            if (!this.promptOpen) {
                this.promptPreviewMode = false;
                this.promptConfigMode = false;
            }
        },

        promptServoNames() {
            const names = {};
            this.tokens.forEach(t => {
                names[t.id] = t.displayName || `servo${t.id}`;
            });
            return names;
        },

        escapeHTML(str) {
            if (!str) return '';
            return str.replace(/[&<>'"]/g, tag => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
            }[tag] || tag));
        },

        promptBackdropHTML() {
            let out = this.escapeHTML(this.promptText);
            out = out.replace(/\{(servo\d+|target_key)\}/g, match => `<mark class="hl-placeholder-bg">${match}</mark>`);
            if (out.endsWith('\n')) out += ' ';
            return out;
        },

        promptPreviewText() {
            let out = this.promptText || '';
            const names = this.promptServoNames();

            Object.entries(names).forEach(([id, name]) => {
                out = out.split(`{servo${id}}`).join(name);
            });

            return out;
        },

        promptPreviewHTML() {
            let out = this.escapeHTML(this.promptText || '');
            const names = this.promptServoNames();

            Object.entries(names).forEach(([id, name]) => {
                const safeName = this.escapeHTML(name);
                out = out.split(`{servo${id}}`).join(`<mark class="hl-placeholder-bg">${safeName}</mark>`);
            });

            out = out.replace(/\{target_key\}/g, '<mark class="hl-placeholder-bg">{target_key}</mark>');
            if (out.endsWith('\n')) out += ' ';
            return out;
        },

        async savePrompt() {
            const res = await this.post('/set_prompt', { prompt_template: this.promptText });
            if (res.ok) {
                this.savedPromptText = this.promptText;
            }
        },

        resetPrompt() {
            this.promptText = this.defaultPromptText;
        },

        // ── model configuration helpers ────────────────────────────
        checkModelsDirty() {
            // Smart validation: compares workspace against actual backend configuration
            const savedStr = JSON.stringify(this.config?.models_config || [
                { model: 'gemini-3.1-flash-lite', priority: true },
                { model: 'gemini-3.5-flash', priority: true }
            ]);
            const currentStr = JSON.stringify(this.localModelsConfig);
            this.modelsConfigDirty = (savedStr !== currentStr);
        },

        canAddModelRow() {
            return this.localModelsConfig.length < this.MODEL_OPTIONS.length;
        },

        addModelRow() {
            const currentModels = this.localModelsConfig.map(m => m.model);
            const nextAvailable = this.MODEL_OPTIONS.find(m => !currentModels.includes(m));
            if (!nextAvailable) return;
            this.localModelsConfig.push({ model: nextAvailable, priority: false });
            this.checkModelsDirty();
        },

        resetModelsConfig() {
            // Reverts back to factory settings, then checks if that actually differs from saved backend data
            this.localModelsConfig = [
                { model: 'gemini-3.1-flash-lite', priority: true },
                { model: 'gemini-3.5-flash', priority: true }
            ];
            this.checkModelsDirty();
        },

        deleteModel(index) {
            this.localModelsConfig.splice(index, 1);
            this.checkModelsDirty();
        },

        updateModelValue(index, value) {
            this.localModelsConfig[index].model = value;
            this.checkModelsDirty();
        },

        moveModelUp(index) {
            if (index > 0) {
                const arr = this.localModelsConfig;
                const temp = arr[index];
                arr[index] = arr[index - 1];
                arr[index - 1] = temp;
                this.checkModelsDirty();
            }
        },

        moveModelDown(index) {
            if (index < this.localModelsConfig.length - 1) {
                const arr = this.localModelsConfig;
                const temp = arr[index];
                arr[index] = arr[index + 1];
                arr[index + 1] = temp;
                this.checkModelsDirty();
            }
        },

        async saveModelsConfig() {
            const res = await this.post('/set_models_config', { models_config: this.localModelsConfig });
            if (res.ok) {
                // Keep the runtime config synchronized with what we just saved
                if (!this.config) this.config = {};
                this.config.models_config = JSON.parse(JSON.stringify(this.localModelsConfig));
                this.checkModelsDirty();
            }
        },
    };
}
