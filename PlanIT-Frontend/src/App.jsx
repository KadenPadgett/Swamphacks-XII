import React, {
  useState,
  useEffect,
  useRef,
  useCallback,
  lazy,
  Suspense,
} from "react";
import { Icon, Badge, Empty, Modal, Forecast, HazardCards } from "./ui.jsx";
import {
  HAZARDS,
  LABELS,
  level,
  sortRows,
  scoreOf,
  countdownState,
  num,
  title,
  ageLabel,
  dateLabel,
  hourLabel,
  flattenEvidence,
  boundText,
  initialAssessmentLabel,
  weatherModelView,
} from "./model.mjs";
import { seedDemo, DEMO_NOW } from "./demo.mjs";
import { configureApi, request, post, apiUrl } from "./api.mjs";
const SiteMap = lazy(() => import("./Map.jsx"));
const NAV = [
  ["overview", "LayoutDashboard", "Overview"],
  ["sites", "MapPinned", "All sites"],
  ["alerts", "Bell", "Alerts"],
  ["reports", "MessageSquareText", "Crew reports"],
  ["briefings", "NotebookText", "Daily briefings"],
];
const EVENTS = [
  "alert_created",
  "alert_updated",
  "alert_resolved",
  "crew_report_created",
  "crew_report_resolved",
  "briefing",
];
function readSetting(key, fallback) {
  try {
    return localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}
function remember(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch {}
}
function emptyDetail(site) {
  return {
    site,
    rule_based_assessment: { current: null, hourly: [] },
    alerts: [],
    active_crew_reports: [],
    required_safety_actions: [],
    freshness: { stale: true },
    ml_predictions: { current_score: null, availability: "unavailable" },
  };
}
export default function App() {
  const [demo, setDemo] = useState(seedDemo),
    [mode, setMode] = useState("demo"),
    [liveRows, setLiveRows] = useState([]),
    [liveDetail, setLiveDetail] = useState(null),
    [selected, setSelected] = useState(1),
    [page, setPage] = useState("overview"),
    [siteTab, setSiteTab] = useState("conditions");
  const [query, setQuery] = useState(""),
    [severity, setSeverity] = useState("all"),
    [modal, setModal] = useState(null),
    [mobile, setMobile] = useState(false),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false),
    [error, setError] = useState(""),
    [toast, setToast] = useState(null),
    [connected, setConnected] = useState(false),
    [lastSync, setLastSync] = useState(null),
    [stream, setStream] = useState("idle");
  const [baseUrl, setBaseUrl] = useState(readSetting("planit:base", "")),
    [token, setToken] = useState(""),
    [now, setNow] = useState(Date.now()),
    [showStops, setShowStops] = useState(false),
    [showNeeds, setShowNeeds] = useState(false),
    [serverOffset, setServerOffset] = useState(0),
    [refreshing, setRefreshing] = useState(false),
    [reportFilter, setReportFilter] = useState("active"),
    [reports, setReports] = useState([]),
    [nearby, setNearby] = useState(false),
    [briefings, setBriefings] = useState([]),
    [briefing, setBriefing] = useState(null),
    [tick, setTick] = useState(0);
  const boot = useRef(Date.now()),
    version = useRef(0),
    detailReq = useRef(0),
    portfolioReq = useRef(0),
    toastTimer = useRef(null),
    streamRef = useRef(null),
    scrollRef = useRef(null);
  const notify = useCallback((message, type = "success") => {
    setToast({ message, type });
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), 6000);
  }, []);
  const clock = mode === "demo" ? DEMO_NOW : now + serverOffset;
  const rows = sortRows(mode === "demo" ? demo.sites : liveRows),
    current = rows.find((r) => r.site_id === selected),
    detail =
      mode === "demo"
        ? demo.details[selected]
        : liveDetail?.site?.id === selected
          ? liveDetail
          : null,
    tz = detail?.timezone?.timezone || "America/New_York";
  const site = detail?.site || current?.site,
    visibleRows = rows.filter(
      (r) =>
        (r.site.name.toLowerCase().includes(query.toLowerCase()) ||
          String(r.site_id) === query) &&
        (severity === "all" || r.rule_based_severity === severity) &&
        (!showStops || r.active_alert_status?.stop_work) &&
        (!showNeeds || r.stale || r.rule_based_severity === "unknown"),
    );
  const alertCount = rows.reduce(
      (a, r) => a + (r.active_alert_status?.count || 0),
      0,
    ),
    stopCount = rows.filter((r) => r.active_alert_status?.stop_work).length,
    watchCount = rows.filter((r) => r.rule_based_severity === "watch").length,
    unknownCount = rows.filter(
      (r) => r.stale || r.rule_based_severity === "unknown",
    ).length;
  const countdown = countdownState(detail?.lightning_countdown, clock),
    reportRows =
      mode === "demo"
        ? nearby
          ? Object.values(demo.details).flatMap(
              (d) => d.active_crew_reports || [],
            )
          : demo.details[selected]?.active_crew_reports || []
        : reports;
  const visibleReports = reportRows.filter(
    (r) => reportFilter === "all" || r.status === reportFilter,
  );
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      clearInterval(id);
      clearTimeout(toastTimer.current);
    };
  }, []);
  useEffect(() => {
    configureApi(baseUrl, token);
  }, [baseUrl, token]);
  const loadPortfolio = useCallback(async () => {
    const v = version.current,
      seq = ++portfolioReq.current;
    try {
      const data = await request("/api/risks");
      if (v !== version.current || seq !== portfolioReq.current) return;
      setLiveRows(data.sites || []);
      setConnected(true);
      setLastSync(Date.now());
      setError("");
    } catch (e) {
      if (v === version.current) {
        setConnected(false);
        setError(`Backend connection interrupted. ${e.message}`);
      }
    }
  }, []);
  const loadDetail = useCallback(async (id) => {
    const v = version.current,
      seq = ++detailReq.current;
    try {
      const d = await request(`/api/sites/${id}/detail`);
      if (v === version.current && seq === detailReq.current) {
        setLiveDetail(d);
        const serverTime = Date.parse(d.lightning_countdown?.server_time);
        if (Number.isFinite(serverTime))
          setServerOffset(serverTime - Date.now());
        setLastSync(Date.now());
      }
    } catch (e) {
      if (v === version.current && seq === detailReq.current)
        setError(e.message);
    }
  }, []);
  useEffect(() => {
    if (mode !== "live") return;
    loadPortfolio();
    const t = setInterval(loadPortfolio, 30000);
    return () => clearInterval(t);
  }, [mode, loadPortfolio]);
  useEffect(() => {
    if (mode !== "live" || !liveRows.some((r) => ["pending", "running"].includes(r.initial_assessment?.status))) return;
    const t = setInterval(() => {
      loadPortfolio();
      if (selected) loadDetail(selected);
    }, 3000);
    return () => clearInterval(t);
  }, [mode, liveRows, selected, loadPortfolio, loadDetail]);
  useEffect(() => {
    if (mode !== "live" || !selected) return;
    setLiveDetail(null);
    loadDetail(selected);
    const t = setInterval(() => loadDetail(selected), 15000);
    return () => {
      clearInterval(t);
      detailReq.current++;
    };
  }, [mode, selected, loadDetail]);
  useEffect(() => {
    if (mode !== "live" || !selected) return;
    const cursorKey = `planit:events:${baseUrl}:${selected}`;
    let highest = Number(readSetting(cursorKey, "0")),
      timer;
    const events = new EventSource(
      apiUrl(`/api/sites/${selected}/events?after=${highest}`),
    );
    streamRef.current = events;
    events.onopen = () => setStream("live");
    events.onerror = () => setStream("reconnecting");
    for (const kind of EVENTS)
      events.addEventListener(kind, (e) => {
        const n = Number(e.lastEventId);
        if (n <= highest) return;
        highest = n;
        remember(cursorKey, String(highest));
        clearTimeout(timer);
        timer = setTimeout(() => {
          loadDetail(selected);
          loadPortfolio();
          setTick((x) => x + 1);
        }, 400);
        if (kind === "alert_created")
          notify(
            "New site alert received. Review the required action.",
            "warning",
          );
      });
    return () => {
      clearTimeout(timer);
      events.close();
      streamRef.current = null;
      setStream("idle");
    };
  }, [mode, selected, baseUrl, loadDetail, loadPortfolio, notify]);
  useEffect(() => {
    if (mode !== "live" || !selected || page !== "reports") return;
    let cancelled = false;
    const path =
      nearby && site
        ? `/api/reports/nearby?latitude=${site.latitude}&longitude=${site.longitude}&radius_km=10&limit=100`
        : `/api/sites/${selected}/reports`;
    request(path)
      .then((d) => {
        if (!cancelled) setReports(Array.isArray(d) ? d : d.reports || []);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [mode, selected, page, nearby, tick, site?.latitude, site?.longitude]);
  useEffect(() => {
    if (page !== "briefings") return;
    setBriefing(null);
    if (mode === "demo") {
      setBriefings(demo.briefings[selected] || []);
      return;
    }
    let cancelled = false;
    request(`/api/sites/${selected}/briefings`)
      .then((d) => {
        if (!cancelled) setBriefings(Array.isArray(d) ? d : d.briefings || []);
      })
      .catch((e) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [mode, selected, page, tick, demo.briefings]);
  function navigate(p) {
    setPage(p);
    setMobile(false);
    setQuery("");
    setSeverity("all");
    setShowStops(false);
    setShowNeeds(false);
    scrollRef.current?.scrollTo({ top: 0 });
  }
  function select(id, open = false) {
    setSelected(id);
    setSiteTab("conditions");
    if (open) navigate("site");
  }
  function backToDemo() {
    version.current++;
    setMode("demo");
    setSelected(1);
    setLiveDetail(null);
    setError("");
    setConnected(false);
    setModal(null);
    notify("Demo workspace opened. All data here is illustrative.");
  }
  async function connect(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(e.currentTarget),
      url = String(form.get("base") || "")
        .trim()
        .replace(/\/$/, "");
    const secret = String(form.get("token") || "");
    try {
      if (location.protocol === "file:" && !url)
        throw new Error(
          "This standalone preview has no API proxy. Run the included project with npm run dev, or enter a reachable backend URL.",
        );
      if (url && !/^https?:\/\//.test(url))
        throw new Error(
          "Use a full http:// or https:// address, or leave it blank for the local proxy.",
        );
      configureApi(url, secret);
      await request("/api/health", { timeout: 8000 });
      const result = await request("/api/risks");
      if (!Array.isArray(result.sites))
        throw new Error("This address did not return the PlanIT API contract.");
      version.current++;
      setBaseUrl(url);
      remember("planit:base", url);
      setToken(secret);
      setLiveRows(result.sites);
      setSelected(
        result.sites[0]?.site_id ?? result.sites[0]?.site?.id ?? null,
      );
      setLiveDetail(null);
      setMode("live");
      setConnected(true);
      setLastSync(Date.now());
      setModal(null);
      setPage("overview");
      notify("Connected to your backend. Showing saved site data.");
    } catch (e) {
      setError(e.message);
      configureApi(baseUrl, token);
    } finally {
      setBusy(false);
    }
  }
  async function refresh() {
    if (mode === "demo") {
      notify("Demo scenario is ready. Connect your backend for live updates.");
      return;
    }
    setRefreshing(true);
    await Promise.all([
      loadPortfolio(),
      selected ? loadDetail(selected) : Promise.resolve(),
    ]);
    setRefreshing(false);
  }
  async function assess() {
    if (mode === "demo") {
      notify(
        "Example assessment displayed. Live refresh is available when connected.",
      );
      return;
    }
    setBusy(true);
    try {
      const d = await request(`/api/sites/${selected}/risk`, {
        timeout: 120000,
      });
      setLiveDetail(d);
      await loadPortfolio();
      notify("Site assessment retrieved. Check source freshness below.");
    } catch (e) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }
  async function addSite(e) {
    e.preventDefault();
    setBusy(true);
    const f = new FormData(e.currentTarget),
      payload = {
        name: String(f.get("name")).trim(),
        latitude: Number(f.get("lat")),
        longitude: Number(f.get("lon")),
        timezone: String(f.get("timezone")).trim() || null,
      };
    try {
      let newSite;
      if (mode === "demo") {
        const id = Math.max(...rows.map((r) => r.site_id), 0) + 1;
        newSite = { id, ...payload };
        const d = emptyDetail(newSite);
        setDemo((prev) => ({
          ...prev,
          details: { ...prev.details, [id]: d },
          sites: [
            ...prev.sites,
            {
              site: newSite,
              site_id: id,
              rank: prev.sites.length + 1,
              rule_based_severity: "unknown",
              status: "pending",
              assessment_status: "missing",
              initial_assessment: { status: "pending" },
              coverage_status: "incomplete",
              stale: true,
            },
          ],
        }));
      } else {
        newSite = await post("/api/sites", payload);
        await loadPortfolio();
      }
      setSelected(newSite.id);
      setModal(null);
      setPage("site");
      notify(
        mode === "demo"
          ? "Example site added to this demo session."
          : "Site saved. Assessing in the background.",
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function addReport(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    const f = new FormData(e.currentTarget);
    const payload = {
      hazard_type: f.get("hazard"),
      description: String(f.get("description")).trim(),
      latitude: Number(f.get("lat")),
      longitude: Number(f.get("lon")),
    };
    try {
      const file = f.get("photo");
      let url = null;
      if (file?.size) {
        if (file.size > 5 * 1024 * 1024)
          throw new Error("Photo must be 5 MiB or smaller.");
        if (mode === "live") {
          const photo = await request(`/api/sites/${selected}/photos`, {
            method: "POST",
            body: file,
          });
          payload.photo_reference = photo.photo_reference;
        } else url = URL.createObjectURL(file);
      }
      if (mode === "demo") {
        const report = {
          ...payload,
          id: Date.now(),
          site_id: selected,
          status: "active",
          report_time: new Date(clock).toISOString(),
          photo_url: url,
        };
        setDemo((prev) => ({
          ...prev,
          details: {
            ...prev.details,
            [selected]: {
              ...prev.details[selected],
              active_crew_reports: [
                report,
                ...(prev.details[selected]?.active_crew_reports || []),
              ],
            },
          },
        }));
      } else {
        await post(`/api/sites/${selected}/reports`, payload);
        await loadDetail(selected);
        setTick((t) => t + 1);
      }
      setModal(null);
      notify(
        mode === "demo"
          ? "Report added to the demo session."
          : "Crew report saved and added to the site event stream.",
      );
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function resolveReport(r) {
    setBusy(true);
    try {
      if (mode === "demo") {
        setDemo((prev) => ({
          ...prev,
          details: Object.fromEntries(
            Object.entries(prev.details).map(([id, d]) => [
              id,
              {
                ...d,
                active_crew_reports: (d.active_crew_reports || []).map((x) =>
                  x.id === r.id
                    ? {
                        ...x,
                        status: "resolved",
                        resolved_at: new Date(clock).toISOString(),
                      }
                    : x,
                ),
              },
            ]),
          ),
        }));
      } else {
        await post(`/api/sites/${r.site_id}/reports/${r.id}/resolve`);
        loadDetail(selected);
        setTick((t) => t + 1);
      }
      notify("Report marked resolved. Its history has been preserved.");
    } catch (e) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }
  async function previewBrief() {
    setBusy(true);
    try {
      if (mode === "demo") {
        setBriefing({
          preview: true,
          briefing: {
            site: site,
            target_date: new Date(clock + 86400000).toISOString().slice(0, 10),
            generated_at: new Date(clock).toISOString(),
            required_safety_actions: detail.required_safety_actions,
            active_crew_reports: detail.active_crew_reports,
            example_summary:
              "Plan exposed work for the cooler morning hours. Review soft access routes and the reported wildlife observation before mobilization. Measured lightning coverage remains unknown; no clearance is implied.",
            missing_data: [
              "Trained ML model unavailable",
              "Measured lightning coverage incomplete",
              "HEFS association not configured",
            ],
            hourly_forecast: detail.rule_based_assessment?.hourly || [],
          },
        });
      } else
        setBriefing(await post(`/api/sites/${selected}/briefings/preview`));
      notify("Briefing preview generated. Scheduled delivery is unchanged.");
    } catch (e) {
      notify(e.message, "error");
    } finally {
      setBusy(false);
    }
  }
  function exportBrief() {
    const data = briefing?.briefing || briefing;
    if (!data) return;
    const body = JSON.stringify(data, null, 2),
      url = URL.createObjectURL(new Blob([body], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `PlanIT-briefing-${site?.id || "site"}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function openModal(m) {
    setError("");
    setModal(m);
  }
  const siteSelector = (
    <select
      aria-label="Selected jobsite"
      value={selected || ""}
      onChange={(e) => setSelected(Number(e.target.value))}
    >
      {rows.map((r) => (
        <option key={r.site_id} value={r.site_id}>
          {r.site.name}
        </option>
      ))}
    </select>
  );
  const map = (
    <Suspense
      fallback={
        <div className="map-loading">
          <Icon name="Map" size={30} />
          Preparing your site map…
        </div>
      }
    >
      <SiteMap
        rows={visibleRows}
        selected={selected}
        onSelect={(id) => select(id)}
        reports={detail?.active_crew_reports || []}
      />
    </Suspense>
  );
  return (
    <div className="app-shell">
      {mobile && (
        <div className="sidebar-scrim" onClick={() => setMobile(false)} />
      )}
      <aside className={`sidebar ${mobile ? "open" : ""}`}>
        <a
          href="#"
          className="brand"
          onClick={(e) => {
            e.preventDefault();
            navigate("overview");
          }}
          aria-label="PlanIT home"
        >
          <span className="brand-symbol">
            <Icon name="Waypoints" size={25} />
          </span>
          <span>
            Plan<span className="brand-it">IT</span>
            <small>CONTROL</small>
          </span>
        </a>
        <div className="workspace-label">YOUR WORKSPACE</div>
        <button
          className="workspace-switch"
          onClick={() => openModal("connect")}
        >
          <span className="workspace-icon">
            <Icon name="HardHat" size={19} />
          </span>
          <span>
            Field operations
            <small>
              {mode === "demo" ? "Everglades · example" : "Connected workspace"}
            </small>
          </span>
          <Icon name="ChevronsUpDown" size={14} />
        </button>
        <nav aria-label="Main navigation">
          {NAV.map(([id, icon, label]) => (
            <button
              className={
                page === id || (page === "site" && id === "sites")
                  ? "active"
                  : ""
              }
              key={id}
              onClick={() => navigate(id)}
            >
              <Icon name={icon} size={18} />
              <span>{label}</span>
              {id === "alerts" && alertCount > 0 && <b>{alertCount}</b>}
              {id === "reports" && <span className="nav-new" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="field-note">
            <Icon name="Sprout" size={21} />
            <strong>Know before you go.</strong>
            <p>A shared view of the conditions that matter.</p>
            <span>
              BUILT FOR THE FIELD <Icon name="ArrowUpRight" size={13} />
            </span>
          </div>
          <button className="settings-nav" onClick={() => openModal("connect")}>
            <Icon name="SlidersHorizontal" size={17} /> Connection settings
          </button>
          <div className="profile">
            <span className="avatar">K</span>
            <div>
              Kaden<small>Operations workspace</small>
            </div>
            <span className="profile-dot" />
          </div>
        </div>
      </aside>
      <div className="app-body">
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-btn mobile-toggle"
              aria-label="Open navigation"
              onClick={() => setMobile(true)}
            >
              <Icon name="Menu" />
            </button>
            <Icon name="PanelTop" size={16} />
            <span>Workspace</span>
            <Icon name="ChevronRight" size={13} />
            <strong>
              {page === "site"
                ? "Site workspace"
                : NAV.find((n) => n[0] === page)?.[2]}
            </strong>
          </div>
          <div className="topbar-right">
            <span
              className={`connection-pill ${mode === "demo" ? "demo" : connected ? "live" : "offline"}`}
            >
              <i />
              {mode === "demo"
                ? "Demo data"
                : connected
                  ? "Backend connected"
                  : "Connection lost"}
            </span>
            <button
              className="icon-btn notifications-btn"
              aria-label="View alerts"
              onClick={() => navigate("alerts")}
            >
              <Icon name="Bell" />
              {alertCount > 0 && <i />}
            </button>
            <span className="avatar small">K</span>
          </div>
        </header>
        <main ref={scrollRef}>
          <div className="page-heading">
            <div>
              <div className="eyebrow">
                {mode === "demo"
                  ? "EVERGLADES OPERATIONS · EXAMPLE SCENARIO"
                  : "ENVIRONMENTAL INTELLIGENCE"}
              </div>
              <h1>
                {page === "overview"
                  ? "Every site. A clearer picture."
                  : page === "sites"
                    ? "Your sites, in focus."
                    : page === "site"
                      ? site?.name || "Site workspace"
                      : page === "alerts"
                        ? "The signals that need you."
                        : page === "reports"
                          ? "A shared view from the field."
                          : "A safer start to tomorrow."}
              </h1>
              <p>
                {page === "overview"
                  ? "Know what’s changing. See what matters. Keep your crews one step ahead."
                  : page === "sites"
                    ? "All monitored locations, ordered by rule-based priority."
                    : page === "site"
                      ? "Conditions, required actions, and crew observations in one place."
                      : page === "alerts"
                        ? "Persistent site alerts with clear actions and the evidence behind them."
                        : page === "reports"
                          ? "Turn a field observation into information the whole crew can use."
                          : "Review the next shift before the first crew arrives."}
              </p>
            </div>
            <div className="heading-actions">
              {page === "overview" || page === "sites" ? (
                <>
                  <button
                    className="button secondary"
                    onClick={refresh}
                    disabled={refreshing}
                  >
                    <Icon
                      name="RefreshCw"
                      size={15}
                      className={refreshing ? "spin" : ""}
                    />
                    Refresh view
                  </button>
                  <button
                    className="button primary"
                    onClick={() => openModal("add-site")}
                  >
                    <Icon name="Plus" size={16} />
                    Add site
                  </button>
                </>
              ) : page === "reports" ? (
                <button
                  className="button primary"
                  disabled={!selected}
                  onClick={() => openModal("report")}
                >
                  <Icon name="Plus" size={16} />
                  New report
                </button>
              ) : page === "briefings" ? (
                <button
                  className="button primary"
                  disabled={!selected || busy}
                  onClick={previewBrief}
                >
                  <Icon name="FileText" size={16} />
                  {busy ? "Preparing…" : "Preview briefing"}
                </button>
              ) : (
                <button
                  className="button secondary"
                  disabled={!selected || busy}
                  onClick={assess}
                >
                  <Icon name="RefreshCw" size={15} />
                  {busy ? "Assessing…" : "Refresh assessment"}
                </button>
              )}
            </div>
          </div>
          {mode === "demo" && (
            <div className="demo-banner">
              <Icon name="Sparkles" size={15} />
              <span>
                You’re exploring an example workday.{" "}
                <strong>
                  All site conditions and observations are demo data.
                </strong>
              </span>
              <button onClick={() => openModal("connect")}>
                Connect your backend <Icon name="ArrowRight" size={14} />
              </button>
            </div>
          )}
          {mode === "live" && error && !modal && (
            <div className="error-banner" role="alert">
              <Icon name="WifiOff" size={18} />
              <span>{error} Previously loaded values may be stale.</span>
              <button onClick={() => openModal("connect")}>Settings</button>
            </div>
          )}
          {mode === "live" && !rows.length ? (
            <Empty icon="MapPinned" title="Your workspace is ready">
              Add your first jobsite, or check your backend connection.
              <div className="empty-actions">
                <button
                  className="button primary"
                  onClick={() => openModal("add-site")}
                >
                  Add a site
                </button>
                <button
                  className="button secondary"
                  onClick={() => openModal("connect")}
                >
                  Connection settings
                </button>
              </div>
            </Empty>
          ) : (
            <>
              {(page === "overview" || page === "sites") && (
                <>
                  <div className="stat-grid">
                    <Metric
                      icon="MapPin"
                      label="Monitored sites"
                      value={rows.length}
                      note="One connected workspace"
                    />
                    <Metric
                      icon="ShieldAlert"
                      label="Sites with work restrictions"
                      value={stopCount}
                      note="Review required actions"
                      color="amber"
                      onClick={() => {
                        setShowStops(!showStops);
                        setSeverity("all");
                        setShowNeeds(false);
                      }}
                      active={showStops}
                    />
                    <Metric
                      icon="Activity"
                      label="On watch"
                      value={watchCount}
                      note="Conditions to keep an eye on"
                      onClick={() => {
                        setSeverity(severity === "watch" ? "all" : "watch");
                        setShowStops(false);
                        setShowNeeds(false);
                      }}
                      active={severity === "watch"}
                    />
                    <Metric
                      icon="CloudOff"
                      label="Needs fresh data"
                      value={unknownCount}
                      note="Unknown never means clear"
                      color="gray"
                      onClick={() => {
                        setShowNeeds(!showNeeds);
                        setSeverity("all");
                        setShowStops(false);
                      }}
                      active={showNeeds}
                    />
                  </div>
                  {stopCount > 0 && (
                    <div className="attention-banner">
                      <span className="attention-icon">
                        <Icon name="ShieldAlert" size={21} />
                      </span>
                      <div>
                        <strong>
                          {stopCount}{" "}
                          {stopCount === 1 ? "site needs" : "sites need"} your
                          attention before work continues
                        </strong>
                        <p>
                          Review current hazards and site-specific actions
                          before mobilizing.
                        </p>
                      </div>
                      <button
                        onClick={() => {
                          setSelected(
                            rows.find((r) => r.active_alert_status?.stop_work)
                              ?.site_id,
                          );
                          navigate("alerts");
                        }}
                      >
                        Review alerts <Icon name="ArrowRight" size={15} />
                      </button>
                    </div>
                  )}
                  <section className="panel portfolio-panel">
                    <div className="panel-heading">
                      <div>
                        <h3>
                          <Icon name="Map" size={18} /> Site overview
                        </h3>
                        <p>
                          Rule-based priority · {visibleRows.length} of{" "}
                          {rows.length} sites
                        </p>
                      </div>
                      <div className="portfolio-filters">
                        <div className="search">
                          <Icon name="Search" size={15} />
                          <input
                            aria-label="Search sites"
                            placeholder="Search sites…"
                            value={query}
                            onChange={(e) => setQuery(e.target.value)}
                          />
                          {query && (
                            <button
                              aria-label="Clear search"
                              onClick={() => setQuery("")}
                            >
                              <Icon name="X" size={13} />
                            </button>
                          )}
                        </div>
                        <select
                          aria-label="Filter by risk"
                          value={severity}
                          onChange={(e) => {
                            setSeverity(e.target.value);
                            setShowStops(false);
                            setShowNeeds(false);
                          }}
                        >
                          <option value="all">All risk levels</option>
                          {["severe", "high", "watch", "unknown", "low"].map(
                            (l) => (
                              <option key={l} value={l}>
                                {LABELS[l]}
                              </option>
                            ),
                          )}
                        </select>
                      </div>
                    </div>
                    {page === "overview" ? (
                      <div className="portfolio-split">
                        <div>{map}</div>
                        <div className="site-list">
                          <div className="site-list-top">
                            <span>PRIORITY QUEUE</span>
                            <span>{visibleRows.length} sites</span>
                          </div>
                          {visibleRows.length ? (
                            visibleRows.map((r) => (
                              <button
                                key={r.site_id}
                                className={`site-list-row ${selected === r.site_id ? "selected" : ""}`}
                                onClick={() => select(r.site_id)}
                              >
                                <span
                                  className={`site-number ${r.rule_based_severity}`}
                                >
                                  {r.rank}
                                </span>
                                <div>
                                  <strong>{r.site.name}</strong>
                                  <small>
                                    {r.site.utility ||
                                      r.site.county ||
                                      `${r.site.latitude.toFixed(3)}, ${r.site.longitude.toFixed(3)}`}
                                  </small>
                                  <span className="site-row-meta">
                                    {r.active_alert_status?.stop_work ? (
                                      <>
                                        <Icon name="PauseCircle" size={11} />
                                        Work restrictions
                                      </>
                                    ) : ["pending", "running"].includes(r.initial_assessment?.status) ? (
                                      <>Assessing…</>
                                    ) : r.stale ? (
                                      <>
                                        <Icon name="Clock3" size={11} />
                                        Needs fresh data
                                      </>
                                    ) : (
                                      <>Rule-based assessment</>
                                    )}
                                  </span>
                                </div>
                                <Badge value={r.rule_based_severity} />
                              </button>
                            ))
                          ) : (
                            <Empty title="No matching sites">
                              Try another search or risk filter.
                            </Empty>
                          )}
                          <div className="site-list-footer">
                            <Icon name="Info" size={13} />
                            <span>
                              Missing data is never shown as low risk.
                            </span>
                          </div>
                        </div>
                      </div>
                    ) : (
                      <div className="site-table-wrap">
                        <table className="site-table">
                          <thead>
                            <tr>
                              <th>Priority / site</th>
                              <th>Risk level</th>
                              <th>Active alerts</th>
                              <th>Assessment</th>
                              <th>Generic ML score</th>
                              <th>Experimental warning · 6h</th>
                              <th />
                            </tr>
                          </thead>
                          <tbody>
                            {visibleRows.map((r) => (
                              <tr key={r.site_id}>
                                <td>
                                  <span
                                    className={`table-rank ${r.rule_based_severity}`}
                                  >
                                    {r.rank}
                                  </span>
                                  <span>
                                    <strong>{r.site.name}</strong>
                                    <small>
                                      {r.site.latitude.toFixed(4)},{" "}
                                      {r.site.longitude.toFixed(4)}
                                    </small>
                                  </span>
                                </td>
                                <td>
                                  <Badge value={r.rule_based_severity} />
                                </td>
                                <td>
                                  {r.active_alert_status.count || 0}
                                  {r.active_alert_status.stop_work && (
                                    <small className="text-amber">
                                      Work restrictions
                                    </small>
                                  )}
                                </td>
                                <td>
                                  {["pending", "running", "failed"].includes(r.initial_assessment?.status)
                                    ? initialAssessmentLabel(r.initial_assessment)
                                    : r.stale
                                    ? "Needs fresh data"
                                    : ageLabel(r.generated_at, clock)}
                                </td>
                                <td>
                                  {num(r.current_score)
                                    ? `${r.current_score.toFixed(1)} / 10`
                                    : "Unavailable"}
                                </td>
                                <td><WeatherModelSummary model={r.weather_model} metar={r.metar} /></td>
                                <td>
                                  <button
                                    className="icon-btn"
                                    aria-label={`Open ${r.site.name}`}
                                    onClick={() => select(r.site_id, true)}
                                  >
                                    <Icon name="ArrowUpRight" />
                                  </button>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                        {!visibleRows.length && (
                          <Empty title="No matching sites">
                            Try another search or risk filter.
                          </Empty>
                        )}
                      </div>
                    )}
                  </section>
                </>
              )}
              {(page === "overview" || page === "site") && site && (
                <>
                  <div className="selected-heading">
                    <div>
                      <span className="eyebrow">
                        {page === "overview"
                          ? "SELECTED SITE"
                          : "SITE CONDITIONS"}
                      </span>
                      <h2>
                        {site.name}{" "}
                        <Badge value={current?.rule_based_severity} />
                        {current?.initial_assessment && ["pending", "running", "failed"].includes(current.initial_assessment.status) && (
                          <span className="assessment-label">{initialAssessmentLabel(current.initial_assessment)}</span>
                        )}
                      </h2>
                      <p>
                        <Icon name="MapPin" size={13} />
                        {Math.abs(site.latitude).toFixed(4)}°{" "}
                        {site.latitude < 0 ? "S" : "N"},{" "}
                        {Math.abs(site.longitude).toFixed(4)}°{" "}
                        {site.longitude < 0 ? "W" : "E"}
                        <span>·</span>
                        {["pending", "running"].includes(detail?.initial_assessment?.status)
                          ? "Assessing…"
                          : detail?.freshness?.stale
                          ? "Assessment needs refresh"
                          : `Assessed ${ageLabel(detail?.generated_at, clock)}`}
                      </p>
                    </div>
                    <div className="selected-heading-actions">
                      {page === "overview" ? (
                        <button
                          className="button secondary"
                          onClick={() => select(selected, true)}
                        >
                          Open site workspace{" "}
                          <Icon name="ArrowUpRight" size={15} />
                        </button>
                      ) : (
                        siteSelector
                      )}
                    </div>
                  </div>
                  {page === "site" && (
                    <div className="tabs">
                      {[
                        ["conditions", "Conditions"],
                        ["actions", "Required actions"],
                        ["river", "River context"],
                        ["sources", "Data & model"],
                      ].map(([id, label]) => (
                        <button
                          className={siteTab === id ? "active" : ""}
                          key={id}
                          onClick={() => setSiteTab(id)}
                        >
                          {label}
                          {id === "actions" && !!detail?.alerts?.length && (
                            <span>{detail.alerts.length}</span>
                          )}
                        </button>
                      ))}
                    </div>
                  )}
                  {!detail ? (
                    <div className="loading-panel">
                      <Icon name="LoaderCircle" className="spin" />
                      Loading saved site conditions…
                    </div>
                  ) : (
                    <>
                      {(page === "overview" || siteTab === "conditions") && (
                        <>
                          <WeatherModelPanel detail={detail} tz={tz} />
                          {detail.initial_assessment?.status === "failed" && (
                            <div className="subtle-warning">
                              Initial assessment failed: {detail.initial_assessment.error || "Unknown error"}.
                              {mode === "live" && <button className="text-button" onClick={async () => {
                                try { await post(`/api/sites/${selected}/assessment/retry`); await loadPortfolio(); await loadDetail(selected); }
                                catch (e) { notify(e.message, "error"); }
                              }}>Retry now</button>}
                            </div>
                          )}
                          {detail.freshness?.stale && (
                            <div className="subtle-warning">
                              <Icon name="Clock3" size={16} />
                              These conditions are retained from an older
                              assessment. Refresh before relying on them.
                            </div>
                          )}
                          <HazardCards
                            detail={detail}
                            compact={page === "overview"}
                            onSelect={(h) =>
                              openModal({ type: "hazard", ...h })
                            }
                          />
                          <div className="detail-grid">
                            <Forecast detail={detail} />
                            <section
                              className={`panel lightning-panel ${countdown.status}`}
                            >
                              <div className="panel-heading">
                                <h3>
                                  <Icon name="Zap" size={18} /> Lightning
                                  monitor
                                </h3>
                                <span
                                  className={`tiny-status ${countdown.status}`}
                                >
                                  {countdown.status === "clear"
                                    ? "Clear window"
                                    : countdown.status === "stop_work"
                                      ? "Active hold"
                                      : "Unknown"}
                                </span>
                              </div>
                              <div className="lightning-center">
                                <div className="lightning-orbit">
                                  <Icon
                                    name={
                                      countdown.status === "stop_work"
                                        ? "Timer"
                                        : "Radar"
                                    }
                                    size={31}
                                  />
                                </div>
                                {countdown.status === "stop_work" ? (
                                  <div className="countdown">
                                    {String(
                                      Math.floor(countdown.seconds / 60),
                                    ).padStart(2, "0")}
                                    <span>:</span>
                                    {String(countdown.seconds % 60).padStart(
                                      2,
                                      "0",
                                    )}
                                  </div>
                                ) : (
                                  <h3>
                                    {countdown.status === "clear"
                                      ? "Coverage confirmed"
                                      : "Waiting for coverage"}
                                  </h3>
                                )}
                                <p>{countdown.label}</p>
                              </div>
                              <div className="lightning-facts">
                                <span>
                                  Monitoring radius <b>10 miles</b>
                                </span>
                                <span>
                                  Clearance window <b>30 minutes</b>
                                </span>
                                <span>
                                  Feed access{" "}
                                  <b>
                                    {title(
                                      detail.lightning_countdown
                                        ?.provider_verification?.feed_access ||
                                        "not configured",
                                    )}
                                  </b>
                                </span>
                              </div>
                              <div className="lightning-note">
                                <Icon name="Info" size={13} />
                                An elapsed timer alone does not clear work.
                              </div>
                            </section>
                          </div>
                          <div className="detail-grid lower-grid">
                            <section className="panel action-preview">
                              <div className="panel-heading">
                                <div>
                                  <h3>
                                    <Icon name="ClipboardCheck" size={18} />{" "}
                                    Before the crew starts
                                  </h3>
                                  <p>
                                    Required actions from active site alerts.
                                  </p>
                                </div>
                                <button
                                  className="text-button"
                                  onClick={() => navigate("alerts")}
                                >
                                  View all <Icon name="ArrowRight" size={14} />
                                </button>
                              </div>
                              {detail.required_safety_actions?.length ? (
                                detail.required_safety_actions
                                  .slice(0, 3)
                                  .map((a, i) => (
                                    <div
                                      className="action-row"
                                      key={a.alert_id || i}
                                    >
                                      <span className="action-index">
                                        0{i + 1}
                                      </span>
                                      <div>
                                        <strong>
                                          {HAZARDS[a.hazard]?.label ||
                                            title(a.hazard)}
                                        </strong>
                                        <p>{a.action}</p>
                                      </div>
                                      {a.stop_work && (
                                        <span className="work-tag">
                                          RESTRICTED
                                        </span>
                                      )}
                                    </div>
                                  ))
                              ) : (
                                <div className="quiet-empty">
                                  <Icon name="ClipboardCheck" size={22} />
                                  <div>
                                    <strong>No active required actions</strong>
                                    <p>
                                      Check data coverage and site conditions
                                      before starting work.
                                    </p>
                                  </div>
                                </div>
                              )}
                            </section>
                            <section className="panel crew-preview">
                              <div className="panel-heading">
                                <h3>
                                  <Icon name="MessageSquareText" size={18} />{" "}
                                  From the field
                                </h3>
                                <button
                                  className="icon-btn"
                                  aria-label="Create crew report"
                                  onClick={() => openModal("report")}
                                >
                                  <Icon name="Plus" size={17} />
                                </button>
                              </div>
                              {detail.active_crew_reports?.filter(
                                (r) => r.status === "active",
                              ).length ? (
                                detail.active_crew_reports
                                  .filter((r) => r.status === "active")
                                  .slice(0, 2)
                                  .map((r) => (
                                    <button
                                      className="crew-mini"
                                      key={r.id}
                                      onClick={() => navigate("reports")}
                                    >
                                      <span className="crew-mini-icon">
                                        <Icon
                                          name={
                                            r.hazard_type === "wildlife"
                                              ? "Binoculars"
                                              : "MapPin"
                                          }
                                          size={19}
                                        />
                                      </span>
                                      <div>
                                        <strong>{title(r.hazard_type)}</strong>
                                        <p>{r.description}</p>
                                        <small>
                                          {ageLabel(r.report_time, clock)} ·
                                          Crew observation
                                        </small>
                                      </div>
                                      <Icon name="ChevronRight" size={14} />
                                    </button>
                                  ))
                              ) : (
                                <div className="quiet-empty">
                                  <Icon name="MessageSquare" size={23} />
                                  <p>No open crew observations.</p>
                                </div>
                              )}
                              <button
                                className="crew-view-all"
                                onClick={() => navigate("reports")}
                              >
                                Open crew reports{" "}
                                <Icon name="ArrowRight" size={14} />
                              </button>
                            </section>
                          </div>
                        </>
                      )}
                      {page === "site" && siteTab === "actions" && (
                        <AlertList
                          alerts={detail.alerts || []}
                          tz={tz}
                          onOpen={(a) => openModal({ type: "alert", alert: a })}
                        />
                      )}
                      {page === "site" && siteTab === "river" && (
                        <RiverPanel
                          detail={detail}
                          onInspect={() =>
                            openModal({
                              type: "raw",
                              data: detail.river_forecast,
                              title: "River forecast details",
                            })
                          }
                        />
                      )}
                      {page === "site" && siteTab === "sources" && (
                        <SourcesPanel
                          detail={detail}
                          clock={clock}
                          onInspect={() =>
                            openModal({
                              type: "raw",
                              data: detail.ml_predictions,
                              title: "Model response",
                            })
                          }
                        />
                      )}
                    </>
                  )}
                </>
              )}
              {page === "alerts" && (
                <>
                  <div className="view-toolbar">
                    <div>
                      <Icon name="MapPin" size={16} />
                      {siteSelector}
                    </div>
                    <span className="muted">
                      {detail?.alerts?.length || 0} active alerts at this site
                    </span>
                  </div>
                  <AlertList
                    alerts={detail?.alerts || []}
                    tz={tz}
                    onOpen={(a) => openModal({ type: "alert", alert: a })}
                  />
                  <p className="page-note">
                    <Icon name="Info" size={14} />
                    These are in-app alerts. Resolving an observation does not
                    automatically clear a hazard or stop-work restriction.
                  </p>
                </>
              )}
              {page === "reports" && (
                <>
                  <div className="view-toolbar">
                    <div>
                      <Icon name="MapPin" size={16} />
                      {siteSelector}
                      <button
                        className={`filter-button ${nearby ? "selected" : ""}`}
                        onClick={() => setNearby(!nearby)}
                      >
                        <Icon name="LocateFixed" size={14} />
                        {nearby ? "Within 10 km" : "This site"}
                      </button>
                    </div>
                    <div className="segmented">
                      {["active", "resolved", "all"].map((k) => (
                        <button
                          key={k}
                          className={reportFilter === k ? "active" : ""}
                          onClick={() => setReportFilter(k)}
                        >
                          {title(k)}
                        </button>
                      ))}
                    </div>
                  </div>
                  {nearby && mode === "demo" && (
                    <p className="page-note">
                      Demo mode shows example observations across the sample
                      workspace. Live mode searches within 10 km.
                    </p>
                  )}
                  <div className="report-grid">
                    {visibleReports.map((r) => (
                      <article
                        className="panel report-card"
                        key={`${r.site_id}-${r.id}`}
                      >
                        <div className="report-card-top">
                          <span className="report-symbol">
                            <Icon
                              name={
                                r.hazard_type === "wildlife"
                                  ? "Binoculars"
                                  : r.hazard_type === "flooding"
                                    ? "Waves"
                                    : "MapPin"
                              }
                              size={22}
                            />
                          </span>
                          <Badge
                            value={r.status === "resolved" ? "low" : "info"}
                          >
                            {r.status === "resolved"
                              ? "Resolved"
                              : "Open observation"}
                          </Badge>
                        </div>
                        <h3>{title(r.hazard_type)}</h3>
                        <p>{r.description}</p>
                        {r.photo_url && (
                          <img
                            className="report-photo"
                            src={
                              r.photo_url.startsWith("blob:")
                                ? r.photo_url
                                : apiUrl(r.photo_url)
                            }
                            alt={`Crew observation: ${r.hazard_type}`}
                            onError={(e) => {
                              e.currentTarget.alt = "Photo unavailable";
                            }}
                          />
                        )}
                        <div className="report-meta">
                          <span>
                            <Icon name="Clock3" size={13} />
                            {dateLabel(r.report_time, tz)}
                          </span>
                          <span>
                            <Icon name="MapPin" size={13} />
                            {r.latitude.toFixed(4)}, {r.longitude.toFixed(4)}
                            {num(r.distance_km)
                              ? ` · ${r.distance_km.toFixed(1)} km`
                              : ""}
                          </span>
                          {r.original_site?.name && (
                            <span>{r.original_site.name}</span>
                          )}
                        </div>
                        <div className="report-footer">
                          <span>Crew observation · unverified</span>
                          {r.status === "active" && (
                            <button
                              className="text-button"
                              disabled={busy}
                              onClick={() =>
                                openModal({ type: "resolve", report: r })
                              }
                            >
                              Resolve <Icon name="Check" size={14} />
                            </button>
                          )}
                        </div>
                      </article>
                    ))}
                  </div>
                  {!visibleReports.length && (
                    <Empty icon="MessageSquare" title="No reports in this view">
                      Share a field observation to give the next crew a better
                      picture.
                      <button
                        className="button primary"
                        onClick={() => openModal("report")}
                      >
                        Create report
                      </button>
                    </Empty>
                  )}
                </>
              )}
              {page === "briefings" && (
                <>
                  <div className="view-toolbar">
                    <div>
                      <Icon name="MapPin" size={16} />
                      {siteSelector}
                    </div>
                    <span className="muted">
                      <Icon name="Clock3" size={14} />
                      Scheduled at 6 PM · site timezone
                    </span>
                  </div>
                  <div className="briefing-layout">
                    <section className="panel briefing-document">
                      <div className="briefing-cover">
                        <div>
                          <span className="eyebrow">THE NEXT SHIFT</span>
                          <h2>
                            Go into tomorrow
                            <br />
                            with a plan.
                          </h2>
                          <p>
                            A single briefing for the conditions, restrictions,
                            <br className="desktop-br" />
                            and observations that deserve attention.
                          </p>
                        </div>
                        <div className="briefing-art">
                          <Icon name="Sunrise" size={78} strokeWidth={1} />
                          <span />
                          <i />
                        </div>
                      </div>
                      {briefing ? (
                        <>
                          <div className="briefing-result">
                            <div className="briefing-result-title">
                              <h3>
                                {briefing.preview
                                  ? "Briefing preview"
                                  : "Saved briefing"}
                              </h3>
                              <button
                                className="button secondary small"
                                onClick={exportBrief}
                              >
                                <Icon name="Download" size={14} />
                                Export JSON
                              </button>
                            </div>
                            <BriefingContents
                              data={briefing.briefing || briefing}
                            />
                          </div>
                        </>
                      ) : (
                        <div className="briefing-intro">
                          <span className="number-stamp">01</span>
                          <div>
                            <h3>Review before you deliver</h3>
                            <p>
                              Preview tomorrow’s briefing using the latest saved
                              assessment. Previewing keeps the scheduled evening
                              delivery unchanged.
                            </p>
                            <button
                              className="button primary"
                              disabled={busy}
                              onClick={previewBrief}
                            >
                              <Icon name="FileText" size={16} />
                              {busy ? "Preparing…" : "Generate preview"}
                            </button>
                          </div>
                        </div>
                      )}
                    </section>
                    <section className="panel briefing-history">
                      <div className="panel-heading">
                        <h3>
                          <Icon name="History" size={18} /> Previous briefings
                        </h3>
                      </div>
                      {briefings.length ? (
                        briefings.map((b, i) => (
                          <button
                            className="history-row"
                            key={b.id || i}
                            onClick={() => setBriefing(b)}
                          >
                            <Icon name="FileText" size={18} />
                            <span>
                              {b.target_date ||
                                b.briefing?.target_date ||
                                "Saved briefing"}
                              <small>
                                {dateLabel(
                                  b.delivery?.delivered_at ||
                                    b.generated_at ||
                                    b.created_at,
                                  tz,
                                )}
                              </small>
                            </span>
                            <Icon name="ChevronRight" size={15} />
                          </button>
                        ))
                      ) : (
                        <Empty icon="Files" title="A fresh start">
                          Saved evening briefings will appear here. Previews are
                          not deliveries.
                        </Empty>
                      )}
                      <div className="briefing-note">
                        <Icon name="Leaf" size={20} />
                        <p>Good decisions start before the morning huddle.</p>
                      </div>
                    </section>
                  </div>
                </>
              )}
            </>
          )}
          <footer className="page-footer">
            <span>
              <span className="mini-brand" />
              PlanIT Control <i>·</i> Built for a safer workday
            </span>
            <span>
              {mode === "demo"
                ? "Illustrative data · no live safety clearance"
                : `Saved view ${lastSync ? ageLabel(lastSync, now) : "pending"} · ${stream === "live" ? "Site events connected" : stream === "reconnecting" ? "Reconnecting events" : "30s portfolio refresh"}`}
            </span>
          </footer>
        </main>
      </div>
      {toast && (
        <div className={`toast ${toast.type}`} role="status">
          <Icon
            name={
              toast.type === "error"
                ? "CircleAlert"
                : toast.type === "warning"
                  ? "TriangleAlert"
                  : "CheckCircle2"
            }
            size={20}
          />
          <span>{toast.message}</span>
          <button
            className="icon-btn"
            aria-label="Dismiss notification"
            onClick={() => setToast(null)}
          >
            <Icon name="X" size={16} />
          </button>
        </div>
      )}
      {modal && (
        <Modal
          title={
            modal === "connect"
              ? "Connect your workspace"
              : modal === "add-site"
                ? "Add a jobsite"
                : modal === "report"
                  ? "Share a field observation"
                  : modal.type === "hazard"
                    ? HAZARDS[modal.key].label
                    : modal.type === "alert"
                      ? "Alert details"
                      : modal.type === "resolve"
                        ? "Resolve this observation?"
                        : modal.title || "Details"
          }
          subtitle={
            modal === "connect"
              ? "Use your local PlanIT backend, or explore the demo."
              : modal === "report"
                ? site?.name
                : null
          }
          wide={modal.type === "raw"}
          onClose={() => {
            setModal(null);
            setError("");
          }}
        >
          {error && (
            <div className="form-error" role="alert">
              {error}
            </div>
          )}
          {modal === "connect" && (
            <form onSubmit={connect}>
              <div className="connection-explainer">
                <Icon name="Laptop" size={22} />
                <div>
                  <strong>Your backend, your data.</strong>
                  <p>
                    Run this frontend on port 5173 and your Python backend on
                    port 8000. The local proxy handles the connection.
                  </p>
                </div>
              </div>
              <label className="field">
                Backend URL{" "}
                <span>Leave blank for the included local proxy</span>
                <input
                  name="base"
                  placeholder="Local proxy → localhost:8000"
                  defaultValue={baseUrl}
                  autoComplete="url"
                />
              </label>
              <label className="field">
                Write token{" "}
                <span>Only if PLANIT_WRITE_TOKEN is set on your backend</span>
                <input
                  type="password"
                  name="token"
                  defaultValue={token}
                  placeholder="Optional"
                  autoComplete="off"
                />
              </label>
              <p className="form-hint">
                Weather-provider keys stay in your Python backend. The write
                token stays in this browser tab’s memory.
              </p>
              <div className="modal-actions">
                <button
                  type="button"
                  className="button secondary"
                  onClick={backToDemo}
                >
                  Explore demo
                </button>
                <button className="button primary" disabled={busy}>
                  {busy ? (
                    <Icon name="LoaderCircle" className="spin" size={16} />
                  ) : (
                    <Icon name="PlugZap" size={16} />
                  )}{" "}
                  {busy ? "Connecting…" : "Connect backend"}
                </button>
              </div>
            </form>
          )}
          {modal === "add-site" && (
            <form onSubmit={addSite}>
              <label className="field">
                Site name
                <input
                  name="name"
                  required
                  maxLength={180}
                  placeholder="e.g. South access road"
                />
              </label>
              <div className="field-row">
                <label className="field">
                  Latitude
                  <input
                    name="lat"
                    type="number"
                    step="any"
                    min="-90"
                    max="90"
                    required
                    placeholder="25.7500"
                  />
                </label>
                <label className="field">
                  Longitude
                  <input
                    name="lon"
                    type="number"
                    step="any"
                    min="-180"
                    max="180"
                    required
                    placeholder="-80.7000"
                  />
                </label>
              </div>
              <label className="field">
                Timezone
                <input
                  name="timezone"
                  defaultValue="America/New_York"
                  placeholder="America/New_York"
                />
              </label>
              <p className="form-hint">
                New sites begin an assessment immediately and show “Assessing…” while it runs.
                {mode === "demo"
                  ? " This creates a temporary example site."
                  : ""}
              </p>
              <div className="modal-actions">
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => setModal(null)}
                >
                  Cancel
                </button>
                <button className="button primary" disabled={busy}>
                  {busy ? "Saving…" : "Add site"}
                </button>
              </div>
            </form>
          )}
          {modal === "report" && (
            <form onSubmit={addReport}>
              <label className="field">
                Observation type
                <select name="hazard">
                  <option value="wildlife">Wildlife</option>
                  <option value="soil_saturation">Ground conditions</option>
                  <option value="flooding">Flooding / standing water</option>
                  <option value="extreme_heat">Heat stress</option>
                  <option value="lightning">
                    Lightning / thunder observed
                  </option>
                  <option value="other">Other</option>
                </select>
              </label>
              <label className="field">
                What did you observe?
                <textarea
                  name="description"
                  rows="4"
                  required
                  maxLength={4000}
                  placeholder="Describe the location, what you saw, and what the next crew should know."
                />
              </label>
              <div className="field-row">
                <label className="field">
                  Observation latitude
                  <input
                    name="lat"
                    type="number"
                    required
                    step="any"
                    min="-90"
                    max="90"
                    defaultValue={site?.latitude}
                  />
                </label>
                <label className="field">
                  Observation longitude
                  <input
                    name="lon"
                    type="number"
                    required
                    step="any"
                    min="-180"
                    max="180"
                    defaultValue={site?.longitude}
                  />
                </label>
              </div>
              <p className="form-hint">
                Coordinates default to the site. Adjust them to the actual
                observation.
              </p>
              <label className="upload-field">
                <Icon name="Camera" size={21} />
                <span>
                  Add a photo{" "}
                  <small>Optional · JPEG, PNG, WebP · up to 5 MiB</small>
                </span>
                <input
                  type="file"
                  name="photo"
                  accept="image/jpeg,image/png,image/webp"
                />
              </label>
              <div className="modal-actions">
                <button
                  type="button"
                  className="button secondary"
                  onClick={() => setModal(null)}
                >
                  Cancel
                </button>
                <button className="button primary" disabled={busy}>
                  {busy ? "Submitting…" : "Submit observation"}
                </button>
              </div>
            </form>
          )}
          {modal.type === "hazard" && (
            <>
              <div className="hazard-modal-top">
                <Badge value={modal.card?.level} />
                <span>
                  {modal.card?.coverage === "available"
                    ? "Source available"
                    : title(modal.card?.coverage || "unavailable") +
                      " coverage"}
                </span>
              </div>
              <p className="evidence-basis">
                {modal.card?.basis || "No assessment data for this hazard yet."}
              </p>
              <dl className="evidence-list">
                {flattenEvidence(modal.card).map(([k, v]) => (
                  <div key={k}>
                    <dt>{k}</dt>
                    <dd>{v}</dd>
                  </div>
                ))}
              </dl>
              {Object.entries(modal.card?.evidence || {})
                .filter(([k, v]) => k.includes("threshold") && Array.isArray(v))
                .map(([k, v]) => (
                  <p className="threshold-note" key={k}>
                    <strong>{title(k)}:</strong> {v.join(" / ")}
                  </p>
                ))}
              <div className="info-box">
                <Icon name="Info" size={17} />
                <p>
                  {modal.key === "flooding"
                    ? "Flooding is a rule-based screening level. It does not represent a probability that this jobsite will flood."
                    : modal.key === "soil_saturation"
                      ? "Volumetric water content is modeled soil wetness. It is not a saturation percentage or a bearing-capacity measurement."
                      : modal.key === "lightning"
                        ? "Thunderstorm forecasts and measured strike coverage are separate. Unknown measured coverage does not establish clearance."
                        : "This level comes from the backend’s rule-based assessment. Review its evidence and source freshness alongside required actions."}
                </p>
              </div>
              <button
                className="button secondary full"
                onClick={() => {
                  setModal(null);
                  setPage("site");
                  setSiteTab("sources");
                }}
              >
                View source freshness <Icon name="ArrowRight" size={15} />
              </button>
            </>
          )}
          {modal.type === "alert" && (
            <>
              <div className="hazard-modal-top">
                <Badge value={modal.alert.severity} />
                <span>
                  {modal.alert.stop_work
                    ? "Work restriction active"
                    : "Action required"}
                </span>
              </div>
              <h3>{modal.alert.message}</h3>
              <div className="alert-action-box">
                <span className="eyebrow">REQUIRED ACTION</span>
                <p>{modal.alert.required_action}</p>
              </div>
              <dl className="evidence-list">
                <div>
                  <dt>Created</dt>
                  <dd>{dateLabel(modal.alert.created_at, tz)}</dd>
                </div>
                <div>
                  <dt>Updated</dt>
                  <dd>{dateLabel(modal.alert.updated_at, tz)}</dd>
                </div>
                <div>
                  <dt>Basis</dt>
                  <dd>{title(modal.alert.origin)}</dd>
                </div>
              </dl>
              <details className="raw-details">
                <summary>View supporting evidence</summary>
                <pre>{JSON.stringify(modal.alert.evidence, null, 2)}</pre>
              </details>
            </>
          )}
          {modal.type === "resolve" && (
            <>
              <p className="evidence-basis">{modal.report.description}</p>
              <p className="form-hint">
                The original observation, timestamp, location, and photo remain
                in history. This does not clear site hazard alerts.
              </p>
              <div className="modal-actions">
                <button
                  className="button secondary"
                  onClick={() => setModal(null)}
                >
                  Keep open
                </button>
                <button
                  className="button primary"
                  disabled={busy}
                  onClick={async () => {
                    await resolveReport(modal.report);
                    setModal(null);
                  }}
                >
                  Mark resolved
                </button>
              </div>
            </>
          )}
          {modal.type === "raw" && (
            <pre className="raw-json">
              {JSON.stringify(modal.data || {}, null, 2)}
            </pre>
          )}
        </Modal>
      )}
    </div>
  );
}
function Metric({
  icon,
  label,
  value,
  note,
  color = "green",
  onClick,
  active,
}) {
  const Tag = onClick ? "button" : "div";
  return (
    <Tag
      className={`metric ${color} ${active ? "selected" : ""}`}
      onClick={onClick}
    >
      <div className="metric-top">
        <span>{label}</span>
        <Icon name={icon} size={18} />
      </div>
      <div className="metric-value">
        {String(value).padStart(2, "0")}
        <span>
          {color === "amber" && value > 0 ? (
            <Icon name="ArrowUpRight" size={17} />
          ) : null}
        </span>
      </div>
      <small>{note}</small>
    </Tag>
  );
}
function AlertList({ alerts, tz, onOpen }) {
  return alerts.length ? (
    <div className="alert-list">
      {alerts.map((a) => (
        <button
          className={`panel alert-card ${a.severity}`}
          key={a.id}
          onClick={() => onOpen(a)}
        >
          <span className="alert-card-icon">
            <Icon
              name={
                a.hazard === "flooding"
                  ? "Waves"
                  : a.hazard === "lightning_strike"
                    ? "Zap"
                    : HAZARDS[a.hazard]?.icon || "TriangleAlert"
              }
              size={23}
            />
          </span>
          <div className="alert-card-content">
            <div>
              <h3>{HAZARDS[a.hazard]?.label || title(a.hazard)}</h3>
              <Badge value={a.severity} />
              {a.stop_work && (
                <span className="work-tag">WORK RESTRICTION</span>
              )}
            </div>
            <p>{a.message}</p>
            <strong>{a.required_action}</strong>
            <small>
              Updated {dateLabel(a.updated_at, tz)} · {title(a.origin)}
            </small>
          </div>
          <Icon name="ArrowUpRight" size={18} />
        </button>
      ))}
    </div>
  ) : (
    <Empty icon="BellOff" title="No active alerts at this site">
      Review current conditions and data coverage before starting work. Missing
      alerts alone do not establish safety.
    </Empty>
  );
}
export function WeatherModelSummary({ model, metar }) {
  const view = weatherModelView(model, metar);
  return <span className="weather-model-summary">
    {view.probability === null ? view.label : `${(view.probability * 100).toFixed(2)}% · experimental`}
    {view.inputStatus !== "available" && <small>METAR {title(view.inputStatus)}</small>}
  </span>;
}
export function WeatherModelPanel({ detail, tz }) {
  const model = detail?.weather_model || {};
  const metar = detail?.metar || {};
  const view = weatherModelView(model, metar);
  const station = metar.station || model.station || {};
  return <section className="panel weather-model-panel">
    <div className="panel-heading"><h3><Icon name="BrainCircuit" size={18} /> Experimental six-hour severe thunderstorm warning prediction</h3>
      <span className="tiny-status">{view.label}</span></div>
    <p className="weather-model-probability">{view.probability === null ? view.label : `${(view.probability * 100).toFixed(2)}% estimated probability`}</p>
    <p>Experimental estimate of a new NWS Severe Thunderstorm Warning covering this site in the next six hours. Calibration is not established. Site hazard level, ranking, stop-work rules, and lightning clearance use separate evidence.</p>
    <dl className="weather-model-facts">
      <div><dt>Model version</dt><dd>{model.model_version || "Unavailable"}</dd></div>
      <div><dt>Prediction time</dt><dd>{dateLabel(model.prediction_time, tz)}</dd></div>
      <div><dt>Outcome window</dt><dd>{dateLabel(model.outcome_start, tz)} – {dateLabel(model.outcome_end, tz)}</dd></div>
      <div><dt>Input freshness</dt><dd>METAR {title(view.inputStatus)}{view.inputReason ? ` · ${title(view.inputReason)}` : ""}</dd></div>
      <div><dt>Station</dt><dd>{station.station_id || "Unavailable"}{num(station.distance_km) ? ` · ${station.distance_km.toFixed(1)} km` : ""}</dd></div>
      <div><dt>Current observation</dt><dd>{dateLabel(metar.current_observed_at || model.current_observed_at, tz)}</dd></div>
      <div><dt>Three-hour comparison</dt><dd>{dateLabel(metar.prior_observed_at || model.prior_observed_at, tz)}</dd></div>
    </dl>
    {view.reason && <p className="model-reason">Model status: {title(view.reason)}</p>}
    {detail?.initial_assessment?.source_failures?.length > 0 && <p className="model-reason">Some sources did not succeed: {detail.initial_assessment.source_failures.map((f) => `${f.source}: ${f.reason}`).join("; ")}</p>}
  </section>;
}
function SourcesPanel({ detail, clock, onInspect }) {
  const model = detail.ml_predictions || {},
    score = scoreOf(detail);
  return (
    <div className="sources-grid">
      <section className="panel">
        <div className="panel-heading">
          <h3>
            <Icon name="Database" size={18} /> Source freshness
          </h3>
          <span className="muted">Original timestamps</span>
        </div>
        <div className="source-list">
          {Object.entries(detail.freshness?.sources || {}).map(([k, v]) => (
            <div className="source-row" key={k}>
              <span
                className={`source-dot ${v.status === "available" && !v.stale ? "ok" : "unknown"}`}
              />
              <div>
                <strong>{title(k)}</strong>
                <small>Retrieved {dateLabel(v.retrieved_at)}</small>
              </div>
              <span>{v.stale ? "Stale" : title(v.status || "unknown")}</span>
            </div>
          ))}
        </div>
        {!Object.keys(detail.freshness?.sources || {}).length && (
          <Empty title="Sources pending">
            No saved assessment is available.
          </Empty>
        )}
        <div className="source-footer">
          Assessment generated {dateLabel(detail.generated_at)}
          <br />
          Refreshing a view does not refresh source observations.
        </div>
      </section>
      <section className="panel model-panel">
        <div className="panel-heading">
          <h3>
            <Icon name="BrainCircuit" size={18} /> Model prediction
          </h3>
          <span className="tiny-status">
            {title(model.stale ? "stale" : model.availability || "unavailable")}
          </span>
        </div>
        <div className="model-value">
          {score === null ? "—" : score.toFixed(1)}
          <span>/ 10</span>
        </div>
        <p>
          {score === null
            ? "A trained model score is not currently available."
            : "Current output from the connected trained model."}
        </p>
        <div className="info-box">
          <Icon name="Info" size={16} />
          <p>
            Site priority and required actions use backend rules. Missing model
            scores remain unavailable.
          </p>
        </div>
        <button className="text-button" onClick={onInspect}>
          Inspect model details <Icon name="ArrowRight" size={14} />
        </button>
      </section>
    </div>
  );
}
function RiverPanel({ detail, onInspect }) {
  const r = detail.river_forecast || {},
    review = detail.river_exposure || {},
    tz = detail.timezone?.timezone || "America/New_York";
  const sections = [
    ["Maximum over the supplied period", r.period_maxima || []],
    ["At individual forecast times", r.individual_times || []],
  ];
  return (
    <section className="panel river-panel">
      <div className="panel-heading">
        <div>
          <h3>
            <Icon name="Waves" size={20} /> River forecast context
          </h3>
          <p>
            River thresholds are separate from the jobsite flood-risk level.
          </p>
        </div>
        <Badge value={review.site_relevance_verified ? "low" : "unknown"}>
          {review.site_relevance_verified
            ? "Connection reviewed"
            : "Connection unconfirmed"}
        </Badge>
      </div>
      <div className="river-content">
        <div>
          <span className="eyebrow">FORECAST AVAILABILITY</span>
          <h2>{title(r.stale ? "stale" : r.availability || "unavailable")}</h2>
          <p>
            {String(
              r.reason ||
                "Review site relevance, forecast thresholds, and source age before using these bounds.",
            ).replaceAll("_", " ")}
          </p>
          <button className="button secondary" onClick={onInspect}>
            Inspect forecast details <Icon name="ArrowUpRight" size={15} />
          </button>
        </div>
        <div className="river-explainer">
          <Icon name="MapPin" size={28} />
          <h3>A nearby river isn’t the whole story.</h3>
          <p>
            Documented river relevance does not establish jobsite inundation, an
            access-route threshold, or the probability that the work area
            floods.
          </p>
        </div>
      </div>
      {sections.map(([label, values]) => {
        const bounds = values.filter((x) => x.probability_bounds);
        return (
          bounds.length > 0 && (
            <div className="bounds-table" key={label}>
              <strong>{label}</strong>
              {bounds.slice(0, 6).map((b, i) => (
                <div key={i}>
                  <span>
                    <b>
                      {title(
                        b.flood_category || b.category || "River threshold",
                      )}{" "}
                      · {b.threshold?.value ?? b.threshold_value ?? "—"}{" "}
                      {b.threshold?.units ?? b.units ?? ""}
                    </b>
                    <small>
                      {b.location_id} · {b.parameter_id} ·{" "}
                      {b.target_time
                        ? dateLabel(b.target_time, tz)
                        : `${dateLabel(b.period_start, tz)} – ${dateLabel(b.period_end, tz)}`}
                    </small>
                    <small>Issued {dateLabel(b.issued_at, tz)}</small>
                  </span>
                  <strong>{boundText(b)}</strong>
                  <small>
                    {b.stale || r.stale
                      ? "Historical / stale"
                      : title(b.availability || "unknown")}
                  </small>
                </div>
              ))}
            </div>
          )
        );
      })}
    </section>
  );
}
function BriefingContents({ data }) {
  const actions = data.required_safety_actions || data.safety_actions || [],
    missing =
      data.missing_information ||
      data.missing_data ||
      data.missing_data_notices ||
      [];
  const rule = data.rule_based_assessment || {},
    reports = data.unresolved_crew_reports || data.active_crew_reports || [],
    tz = data.timezone || "America/New_York";
  return (
    <div className="briefing-content">
      <div className="briefing-date">
        <Icon name="CalendarDays" size={16} />
        {data.target_date || "Next local workday"}
      </div>
      {data.example_summary && (
        <p className="briefing-summary">{data.example_summary}</p>
      )}
      {rule.highest_known_level && (
        <div className="briefing-outlook">
          <span>
            Next-day rule outlook <Badge value={rule.highest_known_level} />
          </span>
          <small>
            {rule.available_hours ?? 0} of {rule.expected_hours ?? 24} hours
            covered · {title(rule.availability || "unavailable")}
          </small>
          {(rule.highest_risk_periods || []).slice(0, 4).map((period, i) => (
            <p key={i}>
              {dateLabel(period.start, tz)} – {hourLabel(period.end, tz)}
              <small>
                {(period.hazards || [])
                  .map((k) => HAZARDS[k]?.label || title(k))
                  .join(" · ")}
              </small>
            </p>
          ))}
        </div>
      )}
      <h4>Required actions</h4>
      {Array.isArray(actions) && actions.length ? (
        actions.map((a, i) => (
          <p className="briefing-action" key={i}>
            <span>{String(i + 1).padStart(2, "0")}</span>
            {a.action || a.required_action || String(a)}
          </p>
        ))
      ) : (
        <p>No required actions included in this response.</p>
      )}
      {reports.length > 0 && (
        <>
          <h4>Open crew observations</h4>
          {reports.map((r, i) => (
            <p className="briefing-observation" key={r.id || i}>
              <Icon name="MapPin" size={14} />
              {r.description}
            </p>
          ))}
        </>
      )}
      <h4>Data limitations</h4>
      {Array.isArray(missing) && missing.length ? (
        missing.map((m, i) => (
          <p className="missing-line" key={i}>
            <Icon name="CircleDashed" size={13} />
            {typeof m === "string" ? m : JSON.stringify(m)}
          </p>
        ))
      ) : (
        <p>
          Inspect the full briefing for source freshness and model availability.
        </p>
      )}
      <details className="raw-details">
        <summary>Full briefing, forecast and source details</summary>
        <pre>{JSON.stringify(data, null, 2)}</pre>
      </details>
    </div>
  );
}
