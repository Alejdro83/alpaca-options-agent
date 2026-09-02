// Static — mirrors config.py's OptionsRiskLimits defaults on the deployed
// instance (see the agent repo's config.py / README "Parameter
// optimization pass" for how each value was chosen). Not fetched live on
// purpose: these are deliberately hard-coded, non-negotiable gates the LLM
// decision layer cannot override — showing them as a static list is the
// same claim the code itself makes, not a display of "current mutable
// settings."
//
// Re-verified against the real deployed config.py 2026-09-02 (values had
// drifted from an earlier "volume over quality" pass on 2026-08-31 without
// this panel being updated -- delta/DTE/concurrent-cap/profit-target were
// all stale here): delta 0.17->0.13, DTE 10-21->7-14, max concurrent 5->7,
// profit target 50%->30%. No env override / evolved_params.json active on
// the live instance at time of writing, confirmed against MSA2 directly.
const GATES: Array<[string, string]> = [
  ['Delta objetivo (pata corta, crédito)', '0.13'],
  ['Ventana DTE', '7 – 14 días'],
  ['Máx. pérdida por spread', '2% del equity'],
  ['Circuit breaker diario', '-3% de P&L'],
  ['Máx. spreads concurrentes', '7'],
  ['Profit target (crédito)', '30% del crédito recibido'],
  ['Stop (crédito)', '2× el crédito recibido'],
  ['Cierre forzado', 'DTE ≤ 1 o ≤2h para el fin del concurso'],
];

// Directional DEBIT spread overlay (2026-09-02) -- shown as its own group
// since it's a genuinely different structure (buys the near-the-money leg,
// profits from real movement instead of time decay) with its own entry
// bar and exit formula, not just a variant of the credit gates above.
// Signal-strength bar 0.65 -> 0.40 (same day): checked against every real
// decision_journal row since this account's reset -- the highest real
// signal strength ever observed was 0.428, so 0.65 would likely have never
// cleared once; 0.40 is a real, reachable value 2 of those candidates
// would have cleared.
const DEBIT_GATES: Array<[string, string]> = [
  ['Barra de entrada', 'ADX > 35 y fuerza de señal ≥ 0.40'],
  ['Delta objetivo (pata comprada)', '0.60 (rango sano 0.35 – 0.80)'],
  ['Profit target', '30% de la ganancia máxima (ancho − débito pagado)'],
  ['Stop', 'proceeds caen al 50% del débito pagado'],
];

export function RiskGatesPanel() {
  return (
    <section className="mb-4">
      <h2 className="text-sm font-semibold text-gray-300 mb-2">Risk gates (código, no la IA)</h2>
      <div className="rounded-lg border border-gray-800 bg-gray-900/20 p-3">
        <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
          {GATES.map(([label, value]) => (
            <div key={label} className="flex justify-between gap-2">
              <dt className="text-gray-500">{label}</dt>
              <dd className="text-gray-200 text-right">{value}</dd>
            </div>
          ))}
        </dl>
        <p className="text-[11px] text-gray-600 mt-2 mb-2">
          Un candidato que falla cualquiera de estos nunca llega a la capa de
          decisión de la IA.
        </p>
        <div className="pt-2 border-t border-gray-800">
          <p className="text-[11px] font-medium uppercase tracking-wide text-amber-500 mb-1.5">
            Spread de débito direccional (complementario)
          </p>
          <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
            {DEBIT_GATES.map(([label, value]) => (
              <div key={label} className="flex justify-between gap-2">
                <dt className="text-gray-500">{label}</dt>
                <dd className="text-gray-200 text-right">{value}</dd>
              </div>
            ))}
          </dl>
          <p className="text-[11px] text-gray-600 mt-2">
            Compra la pata cercana al dinero (la apuesta direccional real) y
            vende una pata más OTM para reducir el coste — gana con
            movimiento real del subyacente, no con paso del tiempo, la
            exposición opuesta a la vertical de crédito de arriba. Si no
            construye (liquidez/delta), cae de vuelta a la vertical de
            crédito estándar sobre la misma señal.
          </p>
        </div>
      </div>
    </section>
  );
}
