import React, { useEffect, useRef } from "react";
import {
  Circle,
  Clock3,
  Search,
  Zap,
  Activity,
  ArrowRight,
  ArrowUpRight,
  Bell,
  BellOff,
  Binoculars,
  BrainCircuit,
  CalendarDays,
  Camera,
  ChartNoAxesCombined,
  Check,
  CheckCircle2,
  ChevronRight,
  ChevronsUpDown,
  CircleAlert,
  CircleDashed,
  ClipboardCheck,
  CloudOff,
  Database,
  Download,
  FileText,
  Files,
  Flame,
  HardHat,
  History,
  Info,
  Laptop,
  Layers,
  LayoutDashboard,
  Leaf,
  LoaderCircle,
  LocateFixed,
  Map,
  MapPin,
  MapPinned,
  Menu,
  MessageSquare,
  MessageSquareText,
  Minus,
  NotebookText,
  PanelTop,
  PauseCircle,
  PlugZap,
  Plus,
  Radar,
  RefreshCw,
  ScanSearch,
  ShieldAlert,
  SlidersHorizontal,
  Sparkles,
  Sprout,
  Sunrise,
  Thermometer,
  Timer,
  TriangleAlert,
  Waves,
  Waypoints,
  WifiOff,
  Wind,
  X,
} from "lucide-react";
const Icons = {
  Circle,
  Clock3,
  Search,
  Zap,
  Activity,
  ArrowRight,
  ArrowUpRight,
  Bell,
  BellOff,
  Binoculars,
  BrainCircuit,
  CalendarDays,
  Camera,
  ChartNoAxesCombined,
  Check,
  CheckCircle2,
  ChevronRight,
  ChevronsUpDown,
  CircleAlert,
  CircleDashed,
  ClipboardCheck,
  CloudOff,
  Database,
  Download,
  FileText,
  Files,
  Flame,
  HardHat,
  History,
  Info,
  Laptop,
  Layers,
  LayoutDashboard,
  Leaf,
  LoaderCircle,
  LocateFixed,
  Map,
  MapPin,
  MapPinned,
  Menu,
  MessageSquare,
  MessageSquareText,
  Minus,
  NotebookText,
  PanelTop,
  PauseCircle,
  PlugZap,
  Plus,
  Radar,
  RefreshCw,
  ScanSearch,
  ShieldAlert,
  SlidersHorizontal,
  Sparkles,
  Sprout,
  Sunrise,
  Thermometer,
  Timer,
  TriangleAlert,
  Waves,
  Waypoints,
  WifiOff,
  Wind,
  X,
};
import {
  LABELS,
  HAZARDS,
  level,
  dateLabel,
  num,
  hourLabel,
  hazardMetric,
} from "./model.mjs";
export function Icon({ name = "Circle", size = 18, ...props }) {
  const C = Icons[name] || Icons.Circle;
  return <C size={size} strokeWidth={1.7} {...props} />;
}
export function Badge({ value = "unknown", children, className = "" }) {
  return (
    <span className={`badge ${level(value)} ${className}`}>
      <i />
      {children || LABELS[level(value)]}
    </span>
  );
}
export function Empty({ icon = "ScanSearch", title, children, action }) {
  return (
    <div className="empty-state">
      <span>
        <Icon name={icon} size={25} />
      </span>
      <h3>{title}</h3>
      <div className="empty-copy">{children}</div>
      {action}
    </div>
  );
}
export function Modal({ title, subtitle, onClose, children, wide = false }) {
  const box = useRef(null),
    close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const old = document.activeElement;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const el = box.current;
    el.focus();
    function key(e) {
      if (e.key === "Escape") close.current();
      if (e.key === "Tab") {
        const nodes = [
          ...el.querySelectorAll("button,input,select,textarea,a[href]"),
        ].filter((n) => !n.disabled && n.offsetParent !== null);
        if (!nodes.length) {
          e.preventDefault();
          return;
        }
        const first = nodes[0],
          last = nodes.at(-1);
        if (
          e.shiftKey &&
          (document.activeElement === first || document.activeElement === el)
        ) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }
    document.addEventListener("keydown", key);
    return () => {
      document.body.style.overflow = overflow;
      document.removeEventListener("keydown", key);
      old?.focus();
    };
  }, []);
  return (
    <div
      className="modal-overlay"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className={`modal ${wide ? "wide" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-title"
        tabIndex={-1}
        ref={box}
      >
        <header>
          <div>
            <span className="eyebrow">PLANIT CONTROL</span>
            <h2 id="modal-title">{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button
            className="icon-btn"
            aria-label="Close dialog"
            onClick={onClose}
          >
            <Icon name="X" />
          </button>
        </header>
        {children}
      </div>
    </div>
  );
}
export function Forecast({ detail }) {
  const [metric, setMetric] = React.useState("heat"),
    [hours, setHours] = React.useState(12),
    [hover, setHover] = React.useState(null);
  const rows = (detail?.rule_based_assessment?.hourly || []).slice(0, hours),
    tz = detail?.timezone?.timezone || "America/New_York";
  const values = rows.map((r) =>
    metric === "heat"
      ? r.indicators?.extreme_heat?.evidence?.apparent_temperature_f
      : r.indicators?.flooding?.evidence?.preceding_24h_precipitation_in,
  );
  const good = values.filter(num),
    lo = good.length
      ? Math.floor(Math.min(...good) / (metric === "heat" ? 10 : 1)) *
          (metric === "heat" ? 10 : 1) -
        (metric === "heat" ? 5 : 0.2)
      : 0,
    hi = good.length
      ? Math.ceil(Math.max(...good) / (metric === "heat" ? 10 : 1)) *
          (metric === "heat" ? 10 : 1) +
        (metric === "heat" ? 5 : 0.2)
      : 1;
  const x = (i) => 48 + (i / Math.max(rows.length - 1, 1)) * 672,
    y = (v) => 155 - ((v - lo) / (hi - lo || 1)) * 125;
  const segments = [];
  let run = [];
  values.forEach((v, i) => {
    if (num(v)) run.push(`${x(i)},${y(v)}`);
    else if (run.length) {
      segments.push(run);
      run = [];
    }
  });
  if (run.length) segments.push(run);
  const chosen = hover == null ? null : rows[hover];
  return (
    <section className="panel forecast-panel">
      <div className="panel-heading">
        <div>
          <h3>
            <Icon name="ChartNoAxesCombined" size={18} /> The hours ahead
          </h3>
          <p>Plan the work around the weather.</p>
        </div>
        <div className="segmented small">
          <button
            className={hours === 12 ? "active" : ""}
            onClick={() => setHours(12)}
          >
            12h
          </button>
          <button
            className={hours === 24 ? "active" : ""}
            onClick={() => setHours(24)}
          >
            24h
          </button>
        </div>
      </div>
      <div className="chart-menu">
        <button
          className={metric === "heat" ? "active" : ""}
          onClick={() => setMetric("heat")}
        >
          <span className="line-key" />
          Feels like · °F
        </button>
        <button
          className={metric === "rain" ? "active" : ""}
          onClick={() => setMetric("rain")}
        >
          24h rainfall · in
        </button>
        <span className="chart-selected">
          {chosen
            ? `${hourLabel(chosen.time, tz)} · ${num(values[hover]) ? values[hover] : "—"}${metric === "heat" ? "°F" : " in"}`
            : "Hourly forecast"}
        </span>
      </div>
      {good.length ? (
        <>
          <div className="chart-wrap">
            <svg
              viewBox="0 0 760 192"
              role="img"
              aria-label={`${metric === "heat" ? "Apparent temperature" : "Rainfall"} forecast`}
            >
              <defs>
                <linearGradient id="chartFill" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#d8974f" stopOpacity=".20" />
                  <stop offset="100%" stopColor="#d8974f" stopOpacity="0" />
                </linearGradient>
              </defs>
              {[0, 1, 2, 3].map((i) => {
                const v = lo + (hi - lo) * (i / 3);
                return (
                  <g key={i}>
                    <line
                      x1="48"
                      y1={y(v)}
                      x2="720"
                      y2={y(v)}
                      stroke="#e6e8e0"
                      strokeDasharray="3 5"
                    />
                    <text
                      x="32"
                      y={y(v) + 4}
                      textAnchor="end"
                      className="axis-text"
                    >
                      {metric === "heat" ? Math.round(v) : v.toFixed(1)}
                    </text>
                  </g>
                );
              })}
              {metric === "heat" && hi > 110 && lo < 110 && (
                <g>
                  <line
                    x1="48"
                    x2="720"
                    y1={y(110)}
                    y2={y(110)}
                    stroke="#c9634c"
                    strokeDasharray="5 4"
                    opacity=".6"
                  />
                  <text
                    x="716"
                    y={y(110) - 6}
                    textAnchor="end"
                    className="threshold-text"
                  >
                    110°F · severe heat
                  </text>
                </g>
              )}
              {segments.map((seg, i) => (
                <g key={i}>
                  <polygon
                    points={`${seg[0].split(",")[0]},155 ${seg.join(" ")} ${seg.at(-1).split(",")[0]},155`}
                    fill="url(#chartFill)"
                  />
                  <polyline
                    points={seg.join(" ")}
                    fill="none"
                    stroke="#c58a48"
                    strokeWidth="2.5"
                    strokeLinejoin="round"
                  />
                </g>
              ))}
              {rows.map((r, i) => (
                <g key={r.time}>
                  {num(values[i]) && (
                    <circle
                      cx={x(i)}
                      cy={y(values[i])}
                      r={hover === i ? 5 : 3}
                      fill="#fff"
                      stroke="#c58a48"
                      strokeWidth="2"
                    />
                  )}
                  {i % Math.max(1, Math.floor(rows.length / 6)) === 0 && (
                    <text
                      className="axis-text"
                      x={x(i)}
                      y="182"
                      textAnchor="middle"
                    >
                      {hourLabel(r.time, tz)}
                    </text>
                  )}
                  <rect
                    x={x(i) - Math.max(10, 330 / rows.length)}
                    y="15"
                    width={Math.max(20, 660 / rows.length)}
                    height="145"
                    fill="transparent"
                    tabIndex="0"
                    role="button"
                    aria-label={`${hourLabel(r.time, tz)}: ${values[i] ?? "unavailable"}`}
                    onMouseEnter={() => setHover(i)}
                    onFocus={() => setHover(i)}
                    onMouseLeave={() => setHover(null)}
                    onBlur={() => setHover(null)}
                  />
                </g>
              ))}
            </svg>
          </div>
          <div className="risk-strip">
            <span>Rule outlook</span>
            <div>
              {rows.map((r) => (
                <span
                  key={r.time}
                  className={`risk-cell ${level(r.overall?.level)}`}
                  title={`${hourLabel(r.time, tz)}: ${LABELS[level(r.overall?.level)]}`}
                />
              ))}
            </div>
            <span>No probabilities</span>
          </div>
        </>
      ) : (
        <Empty icon="CloudOff" title="Forecast not available">
          Connect a site assessment to see the hours ahead.
        </Empty>
      )}
    </section>
  );
}
export function HazardCards({ detail, onSelect, compact = false }) {
  const cards =
    detail?.rule_based_assessment?.current?.indicators ||
    detail?.hazard_evidence ||
    {};
  const keys = compact
    ? ["extreme_heat", "flooding", "soil_saturation", "lightning"]
    : Object.keys(HAZARDS);
  return (
    <div className={`hazard-grid ${compact ? "compact" : ""}`}>
      {keys.map((k) => {
        const meta = HAZARDS[k],
          card = cards[k],
          m = hazardMetric(k, card);
        return (
          <button
            className={`hazard-card ${level(card?.level)}`}
            key={k}
            onClick={() => onSelect({ key: k, card })}
          >
            <div className="hazard-top">
              <span className="hazard-icon" style={{ color: meta.color }}>
                <Icon name={meta.icon} size={20} />
              </span>
              <Badge value={card?.level} />
            </div>
            <h4>{meta.label}</h4>
            <div className="hazard-value">
              {m.value}
              <small>{m.unit}</small>
            </div>
            <div className="hazard-foot">
              <span>{m.label}</span>
              <Icon name="ArrowUpRight" size={14} />
            </div>
            {card?.coverage !== "available" && (
              <span className="coverage-note">
                {card?.coverage === "partial"
                  ? "Partial coverage"
                  : "Data unavailable"}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
