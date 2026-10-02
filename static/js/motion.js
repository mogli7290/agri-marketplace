// Micro-interactions: the small things that make a page feel alive.
//
// Everything here is additive. If this file fails to load, or the browser lacks
// something it needs, or the user has asked for reduced motion, the page is
// already complete and correct — nothing here is load-bearing.
(function () {
    "use strict";

    const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)");

    // -----------------------------------------------------------------------
    // Count-up for the stat numbers
    // -----------------------------------------------------------------------
    // Formatted to match Django's |intcomma so a number never visibly
    // re-formats while it animates: 1234567 ticks up as 1,234,567 the whole way.
    function groupDigits(value) {
        return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    }

    function animateCount(el) {
        const target = parseFloat(el.dataset.count);
        if (isNaN(target)) return;

        const decimals = parseInt(el.dataset.countDecimals || "0", 10);
        // A currency symbol lives outside the number, so the animation does not
        // have to re-parse it every frame.
        const prefix = el.dataset.prefix || "";
        const duration = 900;
        const start = performance.now();

        function render(value) {
            el.textContent = prefix + (decimals
                ? value.toFixed(decimals)
                : groupDigits(Math.round(value)));
        }

        // The server-rendered number stays correct if this never runs. Only
        // once we know we are going to animate do we reset to zero.
        if (target === 0) {
            render(0);
            return;
        }

        function tick(now) {
            const progress = Math.min((now - start) / duration, 1);
            // easeOutExpo, so it races to the number then settles.
            const eased = progress === 1 ? 1 : 1 - Math.pow(2, -10 * progress);
            render(target * eased);
            if (progress < 1) requestAnimationFrame(tick);
            else render(target);
        }
        requestAnimationFrame(tick);
    }

    function initCounts() {
        if (prefersReduced.matches) return; // leave the server-rendered value
        document.querySelectorAll("[data-count]").forEach(animateCount);
    }

    // -----------------------------------------------------------------------
    // Ripple, so a click that navigates away still feels like it landed
    // -----------------------------------------------------------------------
    function ripple(event) {
        if (prefersReduced.matches) return;

        // Only follow links into a detail page. A ripple on every button press
        // is noise.
        const link = event.target.closest("a[href]");
        if (!link) return;
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;

        const size = Math.max(link.offsetWidth, link.offsetHeight) * 2.2;
        const node = document.createElement("span");
        node.className = "ripple";
        node.style.width = node.style.height = size + "px";
        node.style.left = event.clientX + "px";
        node.style.top = event.clientY + "px";
        document.body.appendChild(node);
        node.addEventListener("animationend", () => node.remove());
    }

    // -----------------------------------------------------------------------
    // Reveal on scroll
    // -----------------------------------------------------------------------
    // CSS already animates these on load. This adds the same treatment to
    // anything further down the page, using IntersectionObserver so the cost
    // is proportional to what is actually seen.
    function initReveals() {
        if (prefersReduced.matches) return;
        if (!("IntersectionObserver" in window)) return;

        const observer = new IntersectionObserver(
            (entries) => {
                entries.forEach((entry) => {
                    if (!entry.isIntersecting) return;
                    entry.target.classList.add("reveal");
                    observer.unobserve(entry.target);
                });
            },
            { rootMargin: "0px 0px -8% 0px", threshold: 0.08 }
        );

        document.querySelectorAll("[data-reveal]").forEach((el) => observer.observe(el));
    }

    // -----------------------------------------------------------------------
    // Phone-friendly tables
    // -----------------------------------------------------------------------
    // Under 768px each row collapses into its own card and the column headers
    // become row labels. Those labels are read straight out of the <thead>, so
    // adding a column to a template needs no matching change in the markup —
    // the label is always whatever the header says.
    function initTables() {
        document.querySelectorAll(".table").forEach((table) => {
            if (table.dataset.labelled === "1") return;
            const headers = Array.from(table.querySelectorAll("thead th"));
            if (!headers.length) return;

            table.querySelectorAll("tbody tr").forEach((row) => {
                Array.from(row.children).forEach((cell, index) => {
                    if (cell.tagName !== "TD") return;
                    // Leave action columns alone: a "Manage" button does not
                    // need a label above it.
                    if (!cell.dataset.label && headers[index] && cell.textContent.trim()) {
                        cell.dataset.label = headers[index].textContent.trim();
                    }
                });
            });

            table.dataset.labelled = "1";
        });
    }

    function boot() {
        initCounts();
        initReveals();
        initTables();
        document.addEventListener("click", ripple, { passive: true });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
})();