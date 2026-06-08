// A decorative "accession card" that anchors the hero's right side — a catalog-stamp motif.
export default function Plate({ serial, lines }: { serial: string; lines: string[] }) {
  return (
    <aside className="plate" aria-hidden="true">
      <div className="plate-perf" />
      <div className="plate-head mono">Accession</div>
      <div className="plate-serial">{serial}</div>
      <div className="plate-lines mono">
        {lines.map((l, i) => (
          <div key={i}>{l}</div>
        ))}
      </div>
      <div className="plate-stamp mono">LOCAL</div>
    </aside>
  );
}
