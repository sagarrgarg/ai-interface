/* AI Command Center — cost, reliability and failure forensics for AI Interface.
 *
 * Charts are hand-rendered SVG rather than a library so the validated palette,
 * the 2px stack gaps and the crosshair tooltip behave exactly as specified.
 */

frappe.pages["ai-command-center"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("AI Command Center"),
		single_column: true,
	});
	new AICommandCenter(page);
};

const API = "ai_interface.api.dashboard.";
const PRESETS = [
	{ key: "today", label: "Today" },
	{ key: "7d", label: "7 days" },
	{ key: "30d", label: "30 days" },
	{ key: "90d", label: "90 days" },
	{ key: "mtd", label: "MTD" },
];
const SERIES_VARS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];

class AICommandCenter {
	constructor(page) {
		this.page = page;
		this.filters = { preset: "30d" };
		this.drill = [];
		this.groupBy = "calling_app";
		this.render_shell();
		this.bind_toolbar();
		this.refresh();
	}

	/* ---------------- shell ---------------- */

	render_shell() {
		this.root = $(`
			<div class="acc-root">
				<div class="acc-filters" data-el="filters"></div>
				<div class="acc-grid">
					<div class="acc-kpis" data-el="kpis"></div>

					<div class="acc-card" data-el="insights-card">
						<div class="acc-card-head"><h3>${__("Insights")}</h3></div>
						<div class="acc-sub">${__("Computed from this window — what to actually do about it.")}</div>
						<div class="acc-insights" data-el="insights"></div>
					</div>

					<div class="acc-card">
						<div class="acc-card-head">
							<h3>${__("Spend over time")}</h3>
							<select data-el="groupby">
								<option value="calling_app">${__("by App")}</option>
								<option value="module">${__("by Module")}</option>
								<option value="function_type">${__("by Function")}</option>
								<option value="model">${__("by Model")}</option>
								<option value="provider">${__("by Provider")}</option>
							</select>
						</div>
						<div class="acc-sub">${__("Stacked daily cost.")}</div>
						<div data-el="timeseries"></div>
					</div>

					<div class="acc-card">
						<div class="acc-card-head"><h3>${__("Cost attribution")}</h3></div>
						<div class="acc-sub">${__("Click a row to drill down: App → Module → DocType → Action.")}</div>
						<div class="acc-crumbs" data-el="crumbs"></div>
						<div data-el="attribution"></div>
					</div>

					<div class="acc-row-3">
						<div class="acc-card">
							<div class="acc-card-head"><h3>${__("Reliability")}</h3></div>
							<div class="acc-sub">${__("Daily success rate against a 99% target.")}</div>
							<div data-el="reliability"></div>
						</div>
						<div class="acc-card">
							<div class="acc-card-head"><h3>${__("Failures by type")}</h3></div>
							<div class="acc-sub">${__("Classified from the provider error.")}</div>
							<div data-el="errtypes"></div>
						</div>
					</div>

					<div class="acc-card">
						<div class="acc-card-head"><h3>${__("Failure heatmap")}</h3></div>
						<div class="acc-sub">${__("Failed calls by module and day — darker means more failures.")}</div>
						<div data-el="heatmap"></div>
					</div>

					<div class="acc-card">
						<div class="acc-card-head"><h3>${__("Model efficiency")}</h3></div>
						<div class="acc-sub">${__("What each model actually costs you per call.")}</div>
						<div class="acc-table-wrap" data-el="models"></div>
					</div>

					<div class="acc-card">
						<div class="acc-card-head"><h3>${__("Recent failures")}</h3></div>
						<div class="acc-sub">${__("Most recent first. Retry replays the original prompt.")}</div>
						<div class="acc-table-wrap" data-el="failures"></div>
					</div>
				</div>
			</div>
		`).appendTo(this.page.main);

		this.el = {};
		this.root.find("[data-el]").each((_i, n) => {
			this.el[$(n).attr("data-el")] = $(n);
		});

		this.tip = $('<div class="acc-tip"></div>').appendTo(document.body);
		this.render_filters();
	}

	render_filters() {
		const presets = PRESETS.map(
			(p) =>
				`<button data-preset="${p.key}" aria-pressed="${
					this.filters.preset === p.key
				}">${p.label}</button>`
		).join("");

		this.el.filters.html(`
			<div class="acc-presets">${presets}</div>
			<select data-filter="calling_app"><option value="">${__("All apps")}</option></select>
			<select data-filter="module"><option value="">${__("All modules")}</option></select>
			<select data-filter="function_type"><option value="">${__("All functions")}</option></select>
			<select data-filter="model"><option value="">${__("All models")}</option></select>
			<select data-filter="display_currency" data-el="currency" title="${__(
				"Currency to display figures in"
			)}"></select>
			<select data-filter="status">
				<option value="">${__("All statuses")}</option>
				<option value="Completed">${__("Completed")}</option>
				<option value="Failed">${__("Failed")}</option>
				<option value="Queued">${__("Queued")}</option>
				<option value="Running">${__("Running")}</option>
			</select>
			<div class="acc-spacer"></div>
		`);
	}

	bind_toolbar() {
		this.page.set_secondary_action(__("Refresh"), () => this.refresh(), "refresh");
		this.page.add_menu_item(__("Open AI Call Log"), () =>
			frappe.set_route("List", "AI Call Log")
		);
		this.page.add_menu_item(__("AI Settings"), () =>
			frappe.set_route("Form", "AI Settings")
		);

		this.el.filters.on("click", "[data-preset]", (e) => {
			this.filters.preset = $(e.currentTarget).attr("data-preset");
			this.el.filters.find("[data-preset]").attr("aria-pressed", "false");
			$(e.currentTarget).attr("aria-pressed", "true");
			this.refresh();
		});

		this.el.filters.on("change", "[data-filter]", (e) => {
			const f = $(e.currentTarget).attr("data-filter");
			const v = $(e.currentTarget).val();
			if (v) this.filters[f] = v;
			else delete this.filters[f];
			this.drill = [];
			this.refresh();
		});

		this.el.groupby.on("change", () => {
			this.groupBy = this.el.groupby.val();
			this.load_timeseries();
		});
	}

	/* Every endpoint reports the currency its figures are in; the UI never
	 * assumes one. Falls back to the base currency before the first response. */
	set_currency(code) {
		if (code) this.currency = code;
	}

	money(v, precision = 4) {
		return format_currency(Number(v || 0), this.currency || "USD", precision);
	}

	call(method, args) {
		return frappe.xcall(API + method, args).catch((e) => {
			console.error("[AI Command Center]", method, e);
			return null;
		});
	}

	get active_filters() {
		return Object.assign({}, this.filters, this.drill_filters());
	}

	drill_filters() {
		const out = {};
		this.drill.forEach((d) => (out[d.dimension] = d.value));
		return out;
	}

	refresh() {
		this.load_summary();
		this.load_insights();
		this.load_timeseries();
		this.load_attribution();
		this.load_reliability();
		this.load_failures();
		this.load_filter_options();
	}

	/* ---------------- KPI strip ---------------- */

	async load_summary() {
		this.el.kpis.html(this.skeleton(5, 78));
		const d = await this.call("get_summary", { filters: this.active_filters });
		if (!d) return;

		this.set_currency(d.currency);
		const money = (v) => frappe.utils.escape_html(this.money(v));
		const num = (v) => frappe.format(v || 0, { fieldtype: "Int" });
		const delta = (pct, invert) => {
			if (pct === null || pct === undefined) return `<span class="acc-delta flat">—</span>`;
			const cls = pct === 0 ? "flat" : (pct > 0) === !invert ? "up" : "down";
			const arrow = pct > 0 ? "↑" : pct < 0 ? "↓" : "";
			return `<span class="acc-delta ${cls}">${arrow} ${Math.abs(pct)}%</span>`;
		};

		const srate = d.success_rate;
		const sclass = srate === null ? "flat" : srate >= 99 ? "good" : srate >= 95 ? "warning" : "critical";
		const slabel = srate === null ? __("No settled calls") : srate + "%";

		this.el.kpis.html(`
			<div class="acc-kpi hero">
				<div class="k-label">${__("Total spend")}</div>
				<div class="k-value">${money(d.cost)}</div>
				<div class="k-foot">${delta(d.cost_delta_pct)} <span>${__("vs previous period")}</span></div>
			</div>
			<div class="acc-kpi">
				<div class="k-label">${__("Calls")}</div>
				<div class="k-value">${num(d.calls)}</div>
				<div class="k-foot">${delta(d.calls_delta_pct)} <span>${num(d.in_flight)} ${__("in flight")}</span></div>
			</div>
			<div class="acc-kpi">
				<div class="k-label">${__("Success rate")}</div>
				<div class="k-value">${slabel}</div>
				<div class="k-foot"><span class="acc-pill ${sclass}">${num(d.failed)} ${__("failed")}</span></div>
			</div>
			<div class="acc-kpi">
				<div class="k-label">${__("p95 latency")}</div>
				<div class="k-value">${num(d.p95_latency_ms)}<span style="font-size:14px;color:var(--ink-muted)">ms</span></div>
				<div class="k-foot"><span>${__("avg")} ${num(d.avg_latency_ms)}ms</span></div>
			</div>
			<div class="acc-kpi">
				<div class="k-label">${__("Tokens")}</div>
				<div class="k-value" style="font-size:20px">${num(d.input_tokens)} <span style="color:var(--ink-muted)">→</span> ${num(d.output_tokens)}</div>
				<div class="k-foot"><span>${money(d.avg_cost_per_call)} ${__("per call")}</span></div>
			</div>
		`);
	}

	/* ---------------- insights ---------------- */

	async load_insights() {
		this.el.insights.html(this.skeleton(3, 54));
		const rows = await this.call("get_insights", { filters: this.active_filters });
		if (!rows) return;
		const icon = { good: "✓", info: "💡", warning: "⚠", critical: "✕" };
		this.el.insights.html(
			rows
				.map(
					(r) => `
			<div class="acc-insight ${r.severity}">
				<div>${icon[r.severity] || "•"}</div>
				<div class="i-body">
					<div class="i-title">${frappe.utils.escape_html(r.title)}</div>
					<div class="i-detail">${frappe.utils.escape_html(r.detail)}</div>
				</div>
			</div>`
				)
				.join("")
		);
	}

	/* ---------------- spend over time ---------------- */

	async load_timeseries() {
		this.el.timeseries.html(this.skeleton(1, 220));
		const d = await this.call("get_timeseries", {
			filters: this.active_filters,
			group_by: this.groupBy,
		});
		if (!d) return;
		this.set_currency(d.currency);
		if (!d.buckets.length) return this.el.timeseries.html(this.empty(__("No calls in this window.")));

		const cs = getComputedStyle(this.root[0]);
		const colors = SERIES_VARS.map((v) => cs.getPropertyValue(v).trim());
		const W = this.el.timeseries.width() || 900;
		const H = 240;
		const pad = { t: 12, r: 12, b: 28, l: 56 };
		const iw = W - pad.l - pad.r;
		const ih = H - pad.t - pad.b;

		const totals = d.buckets.map((_b, i) =>
			d.series.reduce((sum, s) => sum + (s.cost[i] || 0), 0)
		);
		const max = Math.max(...totals, 0.000001);
		const bw = Math.max(iw / d.buckets.length - 4, 2);

		let svg = `<svg class="acc-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${__(
			"Daily AI spend"
		)}">`;

		// y grid + ticks
		for (let g = 0; g <= 4; g++) {
			const y = pad.t + (ih * g) / 4;
			const val = max * (1 - g / 4);
			svg += `<line class="acc-grid-line" x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}"/>`;
			svg += `<text class="acc-axis-label" x="${pad.l - 8}" y="${y + 3}" text-anchor="end">${this.money(val, 
				3
			)}</text>`;
		}

		// stacked bars, 2px surface gap between segments
		d.buckets.forEach((b, i) => {
			const x = pad.l + (iw * i) / d.buckets.length + 2;
			let acc = 0;
			d.series.forEach((s, si) => {
				const v = s.cost[i] || 0;
				if (v <= 0) return;
				const h = (v / max) * ih;
				const y = pad.t + ih - acc - h;
				const isTop = acc + h >= totals[i] - 1e-9;
				svg += `<rect x="${x}" y="${y}" width="${bw}" height="${Math.max(h - 2, 0.5)}"
					fill="${colors[si % colors.length]}"
					rx="${isTop ? 4 : 0}" ry="${isTop ? 4 : 0}"
					data-b="${i}" data-s="${si}" style="cursor:pointer"/>`;
				acc += h;
			});
		});

		// x labels — every nth to avoid collisions
		const step = Math.ceil(d.buckets.length / 8);
		d.buckets.forEach((b, i) => {
			if (i % step) return;
			const x = pad.l + (iw * i) / d.buckets.length + bw / 2 + 2;
			svg += `<text class="acc-axis-label" x="${x}" y="${H - 10}" text-anchor="middle">${b.slice(
				5
			)}</text>`;
		});

		svg += `<line class="acc-baseline" x1="${pad.l}" y1="${pad.t + ih}" x2="${W - pad.r}" y2="${
			pad.t + ih
		}"/></svg>`;

		const legend = d.series
			.map(
				(s, i) =>
					`<span><i style="background:${colors[i % colors.length]}"></i>${frappe.utils.escape_html(
						s.name
					)}</span>`
			)
			.join("");

		this.el.timeseries.html(svg + `<div class="acc-legend">${legend}</div>`);

		const self = this;
		this.el.timeseries.find("rect[data-b]").on("mousemove", function (e) {
			const bi = +$(this).attr("data-b");
			const si = +$(this).attr("data-s");
			const s = d.series[si];
			self.show_tip(
				e,
				`<b>${frappe.utils.escape_html(s.name)}</b><br>${d.buckets[bi]}<br>${self.money(
					s.cost[bi]
				)} · ${s.calls[bi] || 0} ${__("calls")}`
			);
		}).on("mouseleave", () => self.hide_tip());
	}

	/* ---------------- attribution drill-down ---------------- */

	async load_attribution() {
		this.el.attribution.html(this.skeleton(6, 26));
		const dim = this.drill.length
			? this.drill[this.drill.length - 1].next
			: "calling_app";
		if (!dim) return this.el.attribution.html(this.empty(__("Deepest level reached.")));

		const d = await this.call("get_attribution", {
			filters: this.filters,
			dimension: dim,
			parent_filters: this.drill_filters(),
		});
		if (!d) return;
		this.set_currency(d.currency);

		this.render_crumbs();
		if (!d.rows.length) return this.el.attribution.html(this.empty(__("Nothing to attribute here.")));

		const cs = getComputedStyle(this.root[0]);
		const fill = cs.getPropertyValue("--seq-400").trim();
		const max = Math.max(...d.rows.map((r) => r.cost), 0.000001);

		const html = d.rows
			.map((r) => {
				const w = Math.max((r.cost / max) * 100, r.cost > 0 ? 2 : 0);
				const canDrill = !!r.next_dimension && r.label !== "Unattributed";
				const fails = r.failed
					? ` <span class="acc-pill critical">${r.failed}</span>`
					: "";
				return `
				<div class="acc-bar-row">
					<div class="acc-bar-label ${canDrill ? "clickable" : ""}"
						 ${canDrill ? `data-drill="${frappe.utils.escape_html(r.label)}" data-next="${r.next_dimension}"` : ""}
						 title="${frappe.utils.escape_html(r.label)}">${frappe.utils.escape_html(r.label)}</div>
					<div class="acc-bar-track">
						<div class="acc-bar-fill" style="width:${w}%;background:${fill}"></div>
					</div>
					<div class="acc-bar-val">${this.money(r.cost)} · ${r.share}% · ${r.calls}${fails}</div>
				</div>`;
			})
			.join("");

		this.el.attribution.html(`<div class="acc-bars">${html}</div>`);

		const self = this;
		this.el.attribution.on("click", "[data-drill]", function () {
			self.drill.push({
				dimension: dim,
				value: $(this).attr("data-drill"),
				next: $(this).attr("data-next"),
			});
			self.load_attribution();
			self.load_summary();
		});
	}

	render_crumbs() {
		if (!this.drill.length) return this.el.crumbs.html("");
		const parts = this.drill
			.map((d, i) => `<a data-crumb="${i}">${frappe.utils.escape_html(d.value)}</a>`)
			.join(" <span>›</span> ");
		this.el.crumbs.html(`<a data-crumb="-1">${__("All")}</a> <span>›</span> ${parts}`);
		const self = this;
		this.el.crumbs.find("[data-crumb]").on("click", function () {
			self.drill = self.drill.slice(0, +$(this).attr("data-crumb") + 1);
			self.load_attribution();
			self.load_summary();
		});
	}

	/* ---------------- reliability ---------------- */

	async load_reliability() {
		this.el.reliability.html(this.skeleton(1, 190));
		this.el.errtypes.html(this.skeleton(4, 22));
		const d = await this.call("get_reliability", { filters: this.active_filters });
		if (!d) return;
		this.set_currency(d.currency);

		const pts = d.daily.filter((x) => x.success_rate !== null);
		if (!pts.length) {
			this.el.reliability.html(this.empty(__("No settled calls in this window.")));
		} else {
			const cs = getComputedStyle(this.root[0]);
			const line = cs.getPropertyValue("--s1").trim();
			const target = cs.getPropertyValue("--good").trim();
			const W = this.el.reliability.width() || 560;
			const H = 200;
			const pad = { t: 12, r: 12, b: 26, l: 44 };
			const iw = W - pad.l - pad.r;
			const ih = H - pad.t - pad.b;
			const lo = Math.min(90, ...pts.map((p) => p.success_rate));
			const scale = (v) => pad.t + ih - ((v - lo) / (100 - lo || 1)) * ih;
			const step = pts.length > 1 ? iw / (pts.length - 1) : 0;

			let svg = `<svg class="acc-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${__(
				"Daily success rate"
			)}">`;
			[100, 99, 95, Math.round(lo)].forEach((v) => {
				if (v < lo) return;
				const y = scale(v);
				svg += `<line class="acc-grid-line" x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}"/>
					<text class="acc-axis-label" x="${pad.l - 7}" y="${y + 3}" text-anchor="end">${v}%</text>`;
			});
			svg += `<line x1="${pad.l}" y1="${scale(99)}" x2="${W - pad.r}" y2="${scale(
				99
			)}" stroke="${target}" stroke-width="1.5" stroke-dasharray="4 4"/>`;

			const path = pts
				.map((p, i) => `${i ? "L" : "M"}${pad.l + step * i},${scale(p.success_rate)}`)
				.join(" ");
			svg += `<path d="${path}" fill="none" stroke="${line}" stroke-width="2" stroke-linejoin="round"/>`;
			pts.forEach((p, i) => {
				svg += `<circle cx="${pad.l + step * i}" cy="${scale(p.success_rate)}" r="4"
					fill="${line}" stroke="var(--surface-1)" stroke-width="2"
					data-i="${i}" style="cursor:pointer"/>`;
			});
			svg += `</svg>`;
			this.el.reliability.html(svg);

			const self = this;
			this.el.reliability.find("circle[data-i]").on("mousemove", function (e) {
				const p = pts[+$(this).attr("data-i")];
				self.show_tip(
					e,
					`<b>${p.bucket}</b><br>${p.success_rate}% ${__("success")}<br>${p.completed} ok · ${
						p.failed
					} ${__("failed")}<br>${p.avg_latency}ms ${__("avg")}`
				);
			}).on("mouseleave", () => self.hide_tip());
		}

		// model efficiency table
		if (!d.models.length) {
			this.el.models.html(this.empty(__("No model usage yet.")));
		} else {
			const rows = d.models
				.map((m) => {
					const sr = m.success_rate;
					const cls = sr === null ? "" : sr >= 99 ? "good" : sr >= 95 ? "warning" : "critical";
					return `<tr>
						<td>${frappe.utils.escape_html(m.model)}</td>
						<td class="num">${m.calls}</td>
						<td class="num">${this.money(m.cost)}</td>
						<td class="num">${this.money(m.avg_cost, 6)}</td>
						<td class="num">${m.avg_input} → ${m.avg_output}</td>
						<td class="num">${m.avg_latency}ms</td>
						<td>${sr === null ? "—" : `<span class="acc-pill ${cls}">${sr}%</span>`}</td>
					</tr>`;
				})
				.join("");
			this.el.models.html(`<table class="acc-table">
				<thead><tr>
					<th>${__("Model")}</th><th class="num">${__("Calls")}</th><th class="num">${__("Total")}</th>
					<th class="num">${__("Per call")}</th><th class="num">${__("Avg tokens")}</th>
					<th class="num">${__("Latency")}</th><th>${__("Success")}</th>
				</tr></thead><tbody>${rows}</tbody></table>`);
		}
	}

	/* ---------------- failures ---------------- */

	async load_failures() {
		this.el.heatmap.html(this.skeleton(4, 22));
		this.el.failures.html(this.skeleton(5, 22));
		const d = await this.call("get_failures", { filters: this.filters });
		if (!d) return;
		this.set_currency(d.currency);

		const cs = getComputedStyle(this.root[0]);

		// by type — sequential single hue, magnitude comparison
		if (!d.by_type.length) {
			this.el.errtypes.html(this.empty(__("No failures. ✓")));
		} else {
			const fill = cs.getPropertyValue("--critical").trim();
			const max = Math.max(...d.by_type.map((r) => r.calls));
			this.el.errtypes.html(
				`<div class="acc-bars">` +
					d.by_type
						.map(
							(r) => `
				<div class="acc-bar-row" style="grid-template-columns:120px 1fr auto">
					<div class="acc-bar-label">${frappe.utils.escape_html(r.label)}</div>
					<div class="acc-bar-track"><div class="acc-bar-fill"
						style="width:${Math.max((r.calls / max) * 100, 2)}%;background:${fill}"></div></div>
					<div class="acc-bar-val" style="min-width:40px">${r.calls}</div>
				</div>`
						)
						.join("") +
					`</div>`
			);
		}

		// heatmap — module x day, sequential ramp
		if (!d.heatmap.length) {
			this.el.heatmap.html(this.empty(__("No failures to plot. ✓")));
		} else {
			const days = [...new Set(d.heatmap.map((h) => h.bucket))].sort();
			const mods = [...new Set(d.heatmap.map((h) => h.row_label))];
			const index = {};
			let max = 0;
			d.heatmap.forEach((h) => {
				index[h.row_label + "|" + h.bucket] = h.calls;
				max = Math.max(max, h.calls);
			});
			const ramp = ["--seq-100", "--seq-250", "--seq-400", "--seq-550", "--seq-700"].map((v) =>
				cs.getPropertyValue(v).trim()
			);
			const shade = (n) => {
				if (!n) return "var(--plane)";
				return ramp[Math.min(Math.floor((n / max) * ramp.length), ramp.length - 1)];
			};

			const rows = mods
				.map((m) => {
					const cells = days
						.map((day) => {
							const n = index[m + "|" + day] || 0;
							return `<div class="acc-heat-cell" style="background:${shade(n)}"
								data-m="${frappe.utils.escape_html(m)}" data-d="${day}" data-n="${n}"></div>`;
						})
						.join("");
					return `<div class="acc-heat-row" style="grid-template-columns:150px 1fr">
						<div class="acc-heat-label" title="${frappe.utils.escape_html(m)}">${frappe.utils.escape_html(
						m
					)}</div>
						<div class="acc-heat-cells">${cells}</div>
					</div>`;
				})
				.join("");

			this.el.heatmap.html(
				`<div class="acc-heat">${rows}</div>
				 <div class="acc-legend"><span style="color:var(--ink-muted)">${__("Fewer")}</span>
				 ${ramp
						.map((c) => `<span><i style="background:${c}"></i></span>`)
						.join("")}
				 <span style="color:var(--ink-muted)">${__("More")}</span></div>`
			);

			const self = this;
			this.el.heatmap.find(".acc-heat-cell").on("mousemove", function (e) {
				self.show_tip(
					e,
					`<b>${$(this).attr("data-m")}</b><br>${$(this).attr("data-d")}<br>${$(this).attr(
						"data-n"
					)} ${__("failures")}`
				);
			}).on("mouseleave", () => self.hide_tip());
		}

		// recent failures table
		if (!d.recent.length) {
			this.el.failures.html(this.empty(__("No failures in this window. ✓")));
			return;
		}
		const rows = d.recent
			.map((r) => {
				const ref =
					r.reference_doctype && r.reference_name
						? `<a href="/app/${frappe.router.slug(r.reference_doctype)}/${encodeURIComponent(
								r.reference_name
						  )}">${frappe.utils.escape_html(r.reference_doctype)}</a>`
						: `<span style="color:var(--ink-muted)">—</span>`;
				return `<tr>
					<td style="white-space:nowrap">${frappe.datetime.str_to_user(r.creation)}</td>
					<td>${frappe.utils.escape_html(r.calling_app || "—")}</td>
					<td>${frappe.utils.escape_html(r.module || "—")}</td>
					<td>${ref}</td>
					<td>${frappe.utils.escape_html(r.action || "—")}</td>
					<td><span class="acc-pill critical">${frappe.utils.escape_html(r.error_type || "Unknown")}</span></td>
					<td style="max-width:280px">${frappe.utils.escape_html(r.error_excerpt || "")}</td>
					<td><button class="btn btn-xs btn-default" data-retry="${r.name}">${__("Retry")}</button></td>
				</tr>`;
			})
			.join("");

		this.el.failures.html(`<table class="acc-table">
			<thead><tr>
				<th>${__("When")}</th><th>${__("App")}</th><th>${__("Module")}</th><th>${__("DocType")}</th>
				<th>${__("Action")}</th><th>${__("Error")}</th><th>${__("Message")}</th><th></th>
			</tr></thead><tbody>${rows}</tbody></table>`);

		const self = this;
		this.el.failures.on("click", "[data-retry]", function () {
			const btn = $(this);
			btn.prop("disabled", true).text(__("Retrying…"));
			self.call("retry_call", { log_name: btn.attr("data-retry") }).then((r) => {
				if (r) {
					frappe.show_alert({ message: __("Retry queued"), indicator: "green" });
					setTimeout(() => self.refresh(), 1200);
				} else {
					btn.prop("disabled", false).text(__("Retry"));
				}
			});
		});
	}

	/* ---------------- filter options ---------------- */

	async load_filter_options() {
		const opts = await this.call("get_filter_options", { filters: { preset: "90d" } });
		if (!opts) return;
		Object.keys(opts).forEach((field) => {
			const sel = this.el.filters.find(`[data-filter="${field}"]`);
			if (!sel.length || !Array.isArray(opts[field])) return;
			const current =
				this.filters[field] || (field === "display_currency" ? opts.base_currency : "") || "";
			const first = sel.find("option").first();
			sel.html(first);
			opts[field].forEach((v) => {
				sel.append(
					`<option value="${frappe.utils.escape_html(v)}" ${
						v === current ? "selected" : ""
					}>${frappe.utils.escape_html(v)}</option>`
				);
			});
		});
	}

	/* ---------------- helpers ---------------- */

	show_tip(e, html) {
		this.tip
			.html(html)
			.addClass("on")
			.css({ left: e.clientX + 14, top: e.clientY + 14 });
	}

	hide_tip() {
		this.tip.removeClass("on");
	}

	skeleton(n, h) {
		return Array.from({ length: n })
			.map(() => `<div class="acc-skeleton" style="height:${h}px;margin-bottom:8px"></div>`)
			.join("");
	}

	empty(msg) {
		return `<div class="acc-empty">${msg}</div>`;
	}
}
