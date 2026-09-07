/* Site assistant — a corner widget that answers questions about this site.
 *
 * Loads on every desk page but renders nothing unless the server says this user
 * may use it. The permission decision belongs on the server; this only asks.
 */

(function () {
	"use strict";

	const API = "ai_interface.api.assistant.";
	const STORAGE_KEY = "ai_assistant_conversation";

	const ICONS = {
		spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.9 4.9L19 9.8l-4.1 2.9L15.8 18 12 15.2 8.2 18l.9-5.3L5 9.8l5.1-1.9z"/></svg>',
		close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
		plus: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
		send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4z"/></svg>',
	};

	const SUGGESTIONS = [
		"How many users are there?",
		"How many records were created today?",
		"Show me the 5 most recent entries",
	];

	class SiteAssistant {
		constructor(config) {
			this.config = config;
			this.conversation = this.remembered();
			this.busy = false;
			this.build();
			this.bind();
			if (this.conversation) this.restore();
		}

		remembered() {
			try {
				return window.localStorage.getItem(STORAGE_KEY) || null;
			} catch (e) {
				// Private windows and blocked site data both throw here; a lost
				// thread is a smaller problem than a widget that will not open.
				return null;
			}
		}

		remember(name) {
			this.conversation = name || null;
			try {
				if (name) window.localStorage.setItem(STORAGE_KEY, name);
				else window.localStorage.removeItem(STORAGE_KEY);
			} catch (e) {
				/* nothing to do — the thread simply will not survive a reload */
			}
		}

		build() {
			this.launcher = document.createElement("button");
			this.launcher.className = "ai-assistant-launcher";
			this.launcher.setAttribute("aria-expanded", "false");
			this.launcher.setAttribute("aria-label", __("Ask about this site"));
			this.launcher.title = __("Ask about this site");
			this.launcher.innerHTML = ICONS.spark;

			this.panel = document.createElement("div");
			this.panel.className = "ai-assistant-panel";
			this.panel.setAttribute("role", "dialog");
			this.panel.setAttribute("aria-label", __("Site assistant"));
			this.panel.hidden = true;
			this.panel.innerHTML = `
				<div class="ai-assistant-head">
					<div class="ai-assistant-title">${frappe.utils.escape_html(__("Site Assistant"))}</div>
					<button data-act="new" title="${__("New conversation")}" aria-label="${__("New conversation")}">${ICONS.plus}</button>
					<button data-act="close" title="${__("Close")}" aria-label="${__("Close")}">${ICONS.close}</button>
				</div>
				<div class="ai-assistant-body" data-el="body"></div>
				<div class="ai-assistant-foot">
					<form class="ai-assistant-form">
						<textarea class="ai-assistant-input" rows="1" data-el="input"
							placeholder="${__("Ask about this site...")}" maxlength="1000"></textarea>
						<button type="submit" class="ai-assistant-send" data-el="send"
							aria-label="${__("Send")}">${ICONS.send}</button>
					</form>
					<div class="ai-assistant-hint">${frappe.utils.escape_html(
						__("Answers use only data you already have access to.")
					)}</div>
				</div>`;

			document.body.appendChild(this.launcher);
			document.body.appendChild(this.panel);

			this.body = this.panel.querySelector('[data-el="body"]');
			this.input = this.panel.querySelector('[data-el="input"]');
			this.send = this.panel.querySelector('[data-el="send"]');
			this.form = this.panel.querySelector(".ai-assistant-form");
			this.showEmpty();
		}

		bind() {
			this.launcher.addEventListener("click", () => this.open());
			this.panel.querySelector('[data-act="close"]').addEventListener("click", () => this.close());
			this.panel.querySelector('[data-act="new"]').addEventListener("click", () => this.reset());

			this.form.addEventListener("submit", (e) => {
				e.preventDefault();
				this.ask(this.input.value);
			});

			this.input.addEventListener("keydown", (e) => {
				// Enter sends; Shift+Enter is a newline, as in every chat box.
				if (e.key === "Enter" && !e.shiftKey) {
					e.preventDefault();
					this.ask(this.input.value);
				}
			});

			this.input.addEventListener("input", () => {
				this.input.style.height = "auto";
				this.input.style.height = Math.min(this.input.scrollHeight, 120) + "px";
			});

			document.addEventListener("keydown", (e) => {
				if (e.key === "Escape" && !this.panel.hidden) this.close();
			});

			this.body.addEventListener("click", (e) => {
				const chip = e.target.closest(".ai-suggestion");
				if (chip) this.ask(chip.textContent);
			});
		}

		open() {
			this.panel.hidden = false;
			this.launcher.setAttribute("aria-expanded", "true");
			this.input.focus();
			this.scroll();
		}

		close() {
			this.panel.hidden = true;
			this.launcher.setAttribute("aria-expanded", "false");
			this.launcher.focus();
		}

		reset() {
			this.remember(null);
			this.body.innerHTML = "";
			this.showEmpty();
			this.input.focus();
		}

		showEmpty() {
			const chips = SUGGESTIONS.map(
				(s) => `<button class="ai-suggestion">${frappe.utils.escape_html(s)}</button>`
			).join("");
			this.body.innerHTML = `
				<div class="ai-assistant-empty">
					<div>${frappe.utils.escape_html(this.config.greeting || __("Ask me anything about this site."))}</div>
					<div class="ai-suggestions">${chips}</div>
				</div>`;
		}

		async restore() {
			const r = await this.call("get_conversation", { conversation: this.conversation });
			if (!r || !r.conversation || !(r.messages || []).length) {
				this.remember(null);
				return;
			}
			this.body.innerHTML = "";
			r.messages.forEach((m) =>
				this.render(m.role === "User" ? "user" : "assistant", m.content, m.query)
			);
			this.scroll();
		}

		async ask(text) {
			const question = (text || "").trim();
			if (!question || this.busy) return;

			const empty = this.body.querySelector(".ai-assistant-empty");
			if (empty) this.body.innerHTML = "";

			this.render("user", question);
			this.input.value = "";
			this.input.style.height = "auto";
			this.setBusy(true);
			const typing = this.renderTyping();

			const r = await this.call("ask", { question, conversation: this.conversation });

			typing.remove();
			this.setBusy(false);

			if (!r) {
				this.render("error", __("The assistant did not respond. Please try again."));
				return;
			}
			if (r.conversation) this.remember(r.conversation);
			this.render(r.ok ? "assistant" : "error", r.answer, r.query);
			this.input.focus();
		}

		render(kind, text, query) {
			const wrap = document.createElement("div");
			wrap.className = "ai-msg " + (kind === "user" ? "user" : kind === "error" ? "assistant error" : "assistant");

			const bubble = document.createElement("div");
			bubble.className = "ai-msg-bubble";
			bubble.textContent = text || "";
			wrap.appendChild(bubble);

			// Shown so an answer can be checked rather than trusted.
			if (query && query.doctype) {
				const details = document.createElement("details");
				details.className = "ai-msg-query";
				const summary = document.createElement("summary");
				summary.textContent = __("Query: {0}", [query.doctype]);
				const pre = document.createElement("pre");
				pre.textContent = JSON.stringify(query, null, 2);
				details.appendChild(summary);
				details.appendChild(pre);
				wrap.appendChild(details);
			}

			this.body.appendChild(wrap);
			this.scroll();
			return wrap;
		}

		renderTyping() {
			const wrap = document.createElement("div");
			wrap.className = "ai-msg assistant";
			wrap.innerHTML = '<div class="ai-msg-bubble ai-typing"><span></span><span></span><span></span></div>';
			this.body.appendChild(wrap);
			this.scroll();
			return wrap;
		}

		setBusy(busy) {
			this.busy = busy;
			this.send.disabled = busy;
			this.input.disabled = busy;
		}

		scroll() {
			this.body.scrollTop = this.body.scrollHeight;
		}

		call(method, args) {
			return frappe.xcall(API + method, args).catch((e) => {
				console.error("[Site Assistant]", method, e);
				return null;
			});
		}
	}

	function init() {
		if (window.__ai_assistant_started) return;
		window.__ai_assistant_started = true;

		frappe
			.xcall(API + "get_config")
			.then((config) => {
				// Nothing is rendered for a user who may not use it — no launcher,
				// no hint that a feature is being withheld.
				if (config && config.enabled) new SiteAssistant(config);
			})
			.catch(() => {});
	}

	if (window.frappe && frappe.ready) frappe.ready(init);
	else document.addEventListener("DOMContentLoaded", init);
})();
