// Shared front-end helpers for AgriMarket.
(function () {
    "use strict";

    function getCookie(name) {
        const match = document.cookie.match(new RegExp("(^|;\\s*)" + name + "=([^;]*)"));
        return match ? decodeURIComponent(match[2]) : null;
    }

    window.agriPost = function (url, data) {
        return fetch(url, {
            method: "POST",
            headers: {
                "X-CSRFToken": getCookie("csrftoken"),
                "X-Requested-With": "XMLHttpRequest",
            },
            body: data,
            credentials: "same-origin",
        }).then((response) =>
            response.json().then((body) => ({ ok: response.ok, body: body }))
        );
    };

    // Price hint on the listing form.
    const hintButton = document.getElementById("price-hint-btn");
    if (hintButton) {
        hintButton.addEventListener("click", function () {
            const crop = document.getElementById("id_crop");
            const grade = document.getElementById("id_quality_grade");
            const output = document.getElementById("price-hint-output");
            if (!crop || !crop.value) {
                output.textContent = "Choose a crop first.";
                return;
            }
            const form = new FormData();
            form.append("crop", crop.value);
            form.append("quality_grade", grade ? grade.value : "A");
            output.textContent = "Thinking…";
            window.agriPost(hintButton.dataset.url, form).then(({ body }) => {
                if (body.price) {
                    const priceField = document.getElementById("id_price_per_unit");
                    if (priceField && !priceField.value) priceField.value = body.price;
                    output.textContent = "Suggested ₹" + body.price + " (" + body.method + "). " + (body.rationale || "");
                } else {
                    output.textContent = body.rationale || "No suggestion available.";
                }
            });
        });
    }

    // Show/hide toggle on every password field.
    //
    // Progressive enhancement: the field renders masked and fully usable with
    // no JavaScript at all, and this only adds a way to check what was typed
    // on a shared screen. Matching on input[type=password] means new forms get
    // it for free — the login form, both registration fields, and the loop-
    // rendered reset form included. The Django admin has its own templates and
    // never loads this file, so it is untouched.
    Array.prototype.forEach.call(
        document.querySelectorAll("input[type=password]"),
        function (input) {
            if (input.dataset.toggleReady) return;
            input.dataset.toggleReady = "1";

            const wrapper = document.createElement("span");
            wrapper.className = "password-field";
            input.parentNode.insertBefore(wrapper, input);
            wrapper.appendChild(input);

            const button = document.createElement("button");
            button.type = "button";
            button.className = "password-toggle";
            button.setAttribute("aria-label", "Show password");
            button.setAttribute("aria-pressed", "false");
            button.innerHTML = '<i class="bi bi-eye"></i>';

            button.addEventListener("click", function () {
                // Flipping the type keeps the value, so nothing is retyped.
                const shown = input.type === "text";
                input.type = shown ? "password" : "text";
                button.setAttribute("aria-pressed", String(!shown));
                button.setAttribute("aria-label", shown ? "Show password" : "Hide password");
                button.innerHTML = shown
                    ? '<i class="bi bi-eye"></i>'
                    : '<i class="bi bi-eye-slash"></i>';
                input.focus();
            });

            wrapper.appendChild(button);
        }
    );

    // Demand forecast widget.
    const forecastForm = document.getElementById("forecast-form");
    if (forecastForm) {
        forecastForm.addEventListener("submit", function (event) {
            event.preventDefault();
            const result = document.getElementById("forecast-result");
            result.innerHTML = "<em>Analysing market data…</em>";
            window.agriPost(forecastForm.dataset.url, new FormData(forecastForm)).then(({ body }) => {
                if (body.error) {
                    result.textContent = body.error;
                    return;
                }
                result.innerHTML =
                    "<p class='mb-1'><strong>" + body.crop + "</strong> in " + body.region + "</p>" +
                    "<p class='mb-1'>Predicted demand: <strong>" + (body.predicted_quantity || "n/a") + "</strong></p>" +
                    "<p class='mb-1'>Suggested price: <strong>₹" + (body.predicted_price_per_unit || "n/a") + "</strong></p>" +
                    "<p class='mb-1'>Confidence: " + body.confidence + "% · method: " + body.method + "</p>" +
                    "<p class='text-muted small mb-0'>" + body.rationale + "</p>";
            });
        });
    }
})();
