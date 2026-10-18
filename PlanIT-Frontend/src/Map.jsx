import React, { useEffect, useRef, useState } from "react";
import L from "leaflet";
import {
  Layers,
  Plus,
  Minus,
  LocateFixed,
  MapPin,
  Maximize2,
} from "lucide-react";
const COLORS = {
  low: "#547a65",
  watch: "#b59342",
  high: "#d08736",
  severe: "#be5b4e",
  unknown: "#8a9695",
  info: "#8a9695",
};
export default function SiteMap({
  rows,
  selected,
  onSelect,
  reports = [],
  large = false,
}) {
  const element = useRef(null),
    map = useRef(null),
    markers = useRef(null),
    circles = useRef(null),
    tiles = useRef(null),
    rowsRef = useRef(rows),
    callback = useRef(onSelect);
  const [layerMenu, setLayerMenu] = useState(false),
    [range, setRange] = useState(false),
    [basemap, setBasemap] = useState(true),
    [tilesReady, setTilesReady] = useState(false);
  rowsRef.current = rows;
  callback.current = onSelect;
  useEffect(() => {
    const m = L.map(element.current, {
      zoomControl: false,
      scrollWheelZoom: false,
      attributionControl: true,
    }).setView([25.82, -80.68], 9);
    map.current = m;
    m.attributionControl.setPrefix(false);
    m.attributionControl.addAttribution("Natural Earth");
    const land = L.geoJSON(null, {
      style: {
        fillColor: "#e5e9db",
        fillOpacity: 1,
        color: "#bbc8b9",
        weight: 1,
      },
      interactive: false,
    }).addTo(m);
    if (window.__PLANIT_LAND__) land.addData(window.__PLANIT_LAND__);
    else
      fetch("./land.json")
        .then((r) => r.json())
        .then((j) => land.addData(j))
        .catch(() => {});
    m.createPane("basemap");
    m.getPane("basemap").style.zIndex = 250;
    const tile = L.tileLayer(
      "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
      {
        pane: "basemap",
        subdomains: "abcd",
        maxZoom: 19,
        attribution: "© OpenStreetMap © CARTO",
      },
    ).addTo(m);
    tiles.current = tile;
    tile.on("tileload", () => setTilesReady(true));
    markers.current = L.layerGroup().addTo(m);
    circles.current = L.layerGroup().addTo(m);
    const ro = new ResizeObserver(() => m.invalidateSize());
    ro.observe(element.current);
    return () => {
      ro.disconnect();
      m.remove();
      map.current = null;
    };
  }, []);
  useEffect(() => {
    const m = map.current;
    if (!m) return;
    const good = rows.filter(
      (r) =>
        Number.isFinite(r.site?.latitude) && Number.isFinite(r.site?.longitude),
    );
    if (good.length)
      m.fitBounds(
        L.latLngBounds(good.map((r) => [r.site.latitude, r.site.longitude])),
        { padding: [65, 65], maxZoom: 11, animate: false },
      );
  }, [rows.map((r) => r.site_id).join(",")]);
  useEffect(() => {
    if (!map.current || !markers.current) return;
    markers.current.clearLayers();
    circles.current.clearLayers();
    rows.forEach((row) => {
      const s = row.site;
      if (!Number.isFinite(s.latitude) || !Number.isFinite(s.longitude)) return;
      const active = row.site_id === selected,
        c = COLORS[row.rule_based_severity] || COLORS.unknown;
      const icon = L.divIcon({
        className: "",
        html: `<span class="site-map-pin ${active ? "selected" : ""}" style="--pin:${c}"><span>${row.rank || ""}</span></span>`,
        iconSize: [38, 38],
        iconAnchor: [19, 19],
      });
      const marker = L.marker([s.latitude, s.longitude], {
        icon,
        keyboard: true,
        title: s.name,
        zIndexOffset: active ? 500 : 0,
      }).addTo(markers.current);
      marker.on("click", () => callback.current(row.site_id));
      const text = document.createElement("span");
      text.textContent = s.name;
      marker.bindTooltip(text, { direction: "top", offset: [0, -22] });
      if (active && range)
        L.circle([s.latitude, s.longitude], {
          radius: 16093.44,
          color: "#6c8e7e",
          fillColor: "#97b19d",
          fillOpacity: 0.1,
          weight: 1.3,
          dashArray: "5 6",
          interactive: false,
        }).addTo(circles.current);
    });
    reports
      .filter((r) => r.status === "active")
      .forEach((r) => {
        if (!Number.isFinite(r.latitude) || !Number.isFinite(r.longitude))
          return;
        const p = L.circleMarker([r.latitude, r.longitude], {
          radius: 5,
          fillColor: "#fff",
          fillOpacity: 1,
          color: "#826732",
          weight: 2,
        }).addTo(markers.current);
        const t = document.createElement("span");
        t.textContent = r.description;
        p.bindTooltip(t);
      });
  }, [rows, selected, range, reports]);
  useEffect(() => {
    const m = map.current;
    if (!m || !tiles.current) return;
    if (basemap) tiles.current.addTo(m);
    else m.removeLayer(tiles.current);
  }, [basemap]);
  const recenter = () => {
    const good = rowsRef.current.filter((r) =>
      Number.isFinite(r.site.latitude),
    );
    if (good.length)
      map.current.fitBounds(
        good.map((r) => [r.site.latitude, r.site.longitude]),
        { padding: [60, 60], maxZoom: 11 },
      );
  };
  return (
    <div className={`map-shell ${large ? "large" : ""}`}>
      <div
        ref={element}
        className="leaflet-surface"
        aria-label="Interactive jobsite map"
      />
      <div className="map-corner-label">
        <span className="live-dot" /> SITE INTELLIGENCE{" "}
        <span className="map-label-sep">/</span> {rows.length} LOCATIONS
      </div>
      <div className="map-tools">
        <button aria-label="Zoom in" onClick={() => map.current.zoomIn()}>
          <Plus size={17} />
        </button>
        <button aria-label="Zoom out" onClick={() => map.current.zoomOut()}>
          <Minus size={17} />
        </button>
        <span />
        <button aria-label="Fit all sites" onClick={recenter}>
          <LocateFixed size={17} />
        </button>
      </div>
      <div className="map-layer">
        <button
          className="map-layer-button"
          onClick={() => setLayerMenu(!layerMenu)}
        >
          <Layers size={15} /> Layers
        </button>
        {layerMenu && (
          <div className="layers-popover">
            <label>
              <input
                type="checkbox"
                checked={basemap}
                onChange={(e) => setBasemap(e.target.checked)}
              />{" "}
              Street basemap
            </label>
            <label>
              <input
                type="checkbox"
                checked={range}
                onChange={(e) => setRange(e.target.checked)}
              />{" "}
              Selected site · 10 mi radius
            </label>
            <small>
              Radius is a distance reference, not a hazard boundary.
            </small>
          </div>
        )}
      </div>
      <div className="map-legend">
        {["high", "watch", "low", "unknown"].map((k) => (
          <span key={k}>
            <i style={{ background: COLORS[k] }} />
            {k === "unknown" ? "Unknown" : k[0].toUpperCase() + k.slice(1)}
          </span>
        ))}
      </div>
      {(!tilesReady || !basemap) && (
        <span className="map-offline">Offline vector basemap</span>
      )}
    </div>
  );
}
