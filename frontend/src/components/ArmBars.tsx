import { ARM_INFO, ARMS, type Arm } from "../lib";

// Horizontal A/B/C bar comparison for a single metric.
// `higherIsBetter` only affects which bar is highlighted as best.
export default function ArmBars({
  values,
  format,
  higherIsBetter = true,
}: {
  values: Record<string, number | null | undefined>;
  format: (v: number | null | undefined) => string;
  higherIsBetter?: boolean;
}) {
  const nums = ARMS.map((a) => values[a]).filter((v): v is number => typeof v === "number");
  const max = nums.length ? Math.max(...nums, 0.000001) : 1;
  const best = nums.length
    ? higherIsBetter
      ? Math.max(...nums)
      : Math.min(...nums)
    : null;

  return (
    <div className="bar-compare">
      {ARMS.map((a) => {
        const v = values[a];
        const has = typeof v === "number";
        const w = has ? Math.max(2, (Math.abs(v as number) / max) * 100) : 0;
        const isBest = has && best !== null && v === best && nums.length > 1;
        return (
          <div className="bar-row" key={a}>
            <span className="bl">
              <span className={`pill ${a.toLowerCase()}`}>{a}</span>
            </span>
            <div className="bar-track">
              <div
                className="bar-val"
                style={{ width: `${w}%`, background: ARM_INFO[a as Arm].color, opacity: isBest ? 1 : 0.55 }}
              />
            </div>
            <span className="bar-num" style={{ fontWeight: isBest ? 800 : 600 }}>
              {format(v)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
