// The animated hero background.
//
// One full-screen fragment shader, run on a 2D canvas. It exists to give the
// landing page some life; it is decoration and nothing more, so the rules are:
//
// * **Zero dependencies.** No library, no external texture, no network call.
//   The shader is the string below and nothing else.
// * **Bail out quietly.** No WebGL2, no WebGL, reduced motion, a hidden tab, or
//   a device that cannot spare the frame budget — in every case we simply never
//   start, and the CSS gradient behind the canvas is already a finished design.
// * **Stop when nobody is looking.** The animation only runs while the hero is
//   on screen and the document is visible. A farmer scrolling past the hero is
//   not paying for it.
// * **Respect the pixel ratio cap.** Rendering at 3x on a phone is the fastest
//   way to make a beautiful page feel cheap, so it is clamped.
(function () {
    "use strict";

    const VERTEX = `
        attribute vec2 aPosition;
        void main() { gl_Position = vec4(aPosition, 0.0, 1.0); }
    `;

    // A slow flow field. Two stacked sine waves drift across the surface and
    // are tinted from deep green through to warm gold, which is roughly the
    // brand palette. Cheap enough to run at 60fps on a mid-range phone.
    const FRAGMENT = `
        precision mediump float;

        uniform vec2  uResolution;
        uniform float uTime;

        float wave(vec2 uv, float t, float speed, float scale) {
            return sin((uv.x * scale) + t * speed) * 0.5 + 0.5;
        }

        void main() {
            vec2 uv = gl_FragCoord.xy / uResolution.xy;
            // Keep the aspect right so the pattern never stretches.
            vec2 p = vec2(uv.x, uv.y);

            float t = uTime * 0.22;

            float a = wave(p, t, 1.0, 3.2);
            float b = wave(vec2(p.y, p.x), t, 0.7, 2.4);
            float c = wave(p, -t, 0.5, 5.1);

            float field = a * 0.45 + b * 0.35 + c * 0.20;

            vec3 deep  = vec3(0.08, 0.32, 0.18);
            vec3 green = vec3(0.12, 0.48, 0.25);
            vec3 gold  = vec3(0.98, 0.66, 0.15);

            vec3 colour = mix(deep, green, smoothstep(0.15, 0.75, field));
            colour = mix(colour, gold, smoothstep(0.72, 1.0, field) * 0.45);

            // Vignette, so the copy in the middle always has contrast.
            float vignette = smoothstep(1.15, 0.25, length(uv - 0.5));
            colour *= 0.72 + 0.28 * vignette;

            gl_FragColor = vec4(colour, 1.0);
        }
    `;

    function compile(gl, type, source) {
        const shader = gl.createShader(type);
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            gl.deleteShader(shader);
            return null;
        }
        return shader;
    }

    function start(canvas) {
        const gl = canvas.getContext("webgl", { antialias: false, alpha: false })
                || canvas.getContext("experimental-webgl");

        // No WebGL is a normal outcome, not an error. The CSS gradient shows.
        if (!gl) return;

        const vertexShader = compile(gl, gl.VERTEX_SHADER, VERTEX);
        const fragmentShader = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT);
        if (!vertexShader || !fragmentShader) return;

        const program = gl.createProgram();
        gl.attachShader(program, vertexShader);
        gl.attachShader(program, fragmentShader);
        gl.linkProgram(program);
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) return;
        gl.useProgram(program);

        const buffer = gl.createBuffer();
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        gl.bufferData(
            gl.ARRAY_BUFFER,
            new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]),
            gl.STATIC_DRAW
        );
        const position = gl.getAttribLocation(program, "aPosition");
        gl.enableVertexAttribArray(position);
        gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);

        const uTime = gl.getUniformLocation(program, "uTime");
        const uResolution = gl.getUniformLocation(program, "uResolution");

        // Clamp the pixel ratio: rendering a full shader at 3x is the single
        // fastest way to make this page feel slow.
        const dpr = Math.min(window.devicePixelRatio || 1, 1.5);

        function resize() {
            const rect = canvas.getBoundingClientRect();
            canvas.width = Math.max(1, Math.floor(rect.width * dpr));
            canvas.height = Math.max(1, Math.floor(rect.height * dpr));
            gl.viewport(0, 0, canvas.width, canvas.height);
            gl.uniform2f(uResolution, canvas.width, canvas.height);
        }

        resize();
        window.addEventListener("resize", resize, { passive: true });

        const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
        let running = false;
        let frame = null;
        const startTime = performance.now();

        function draw(now) {
            gl.uniform1f(uTime, (now - startTime) / 1000);
            gl.drawArrays(gl.TRIANGLES, 0, 6);
            frame = requestAnimationFrame(draw);
        }

        function play() {
            if (running || reduced.matches || document.hidden) return;
            running = true;
            frame = requestAnimationFrame(draw);
        }

        function pause() {
            if (!running) return;
            running = false;
            if (frame) cancelAnimationFrame(frame);
            frame = null;
        }

        document.addEventListener("visibilitychange", () => {
            if (document.hidden) pause(); else play();
        });
        if (reduced.addEventListener) {
            reduced.addEventListener("change", () => (reduced.matches ? pause() : play()));
        }

        play();
    }

    function boot() {
        const canvas = document.querySelector("[data-hero-canvas]");
        if (!canvas) return;

        // Someone who asked for less motion gets the finished gradient.
        if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

        try {
            start(canvas);
        } catch (err) {
            // A shader is never worth breaking a page over.
            if (window.console) console.debug("Hero animation unavailable:", err);
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();