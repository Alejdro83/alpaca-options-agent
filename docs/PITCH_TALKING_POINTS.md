# Pitch talking points — "code vs. reasoning" segment (~30-40s spoken)

Read as-is or paraphrase in your own words — this is meant to slot into
the pitch video wherever you walk through the architecture or the
/compare panel.

---

> Every AI trading agent has to answer one question: how much does the
> model actually decide, versus code around it? We didn't just answer
> that once — we built two versions and are measuring the difference
> live.
>
> Our judged bot is a deterministic pipeline — screening, regime
> detection, strike selection — all code. The LLM only picks the final
> trade among candidates that already passed every check. In parallel, we
> built Paco: a fully autonomous agent that reasons through the *entire*
> cycle itself — what to look at, which strikes to build, when to act —
> with the exact same risk code as a hard veto, imported directly, never
> reimplemented.
>
> And we can tell you exactly how much freedom that gives it. Our own bot
> never builds a trade outside a 0.02 to 0.32 delta band. Paco's safety
> check allows up to 0.45 — real, measured room to pick a meaningfully
> more aggressive strike, while it's still provably incapable of blowing
> past the portfolio-wide risk caps, which are identical for both.
>
> Every threshold in this system is labeled — backed by real research, or
> flagged as an unvalidated starting point we're watching with a nightly
> evolution job before we'd ever let it touch a real trade. That's on our
> public dashboard too, alongside a third, independently-built
> implementation — three real decision architectures, one shared risk
> brain, compared live.

---

## One-liner version (if time is tight)

> We didn't just build one trading agent — we built two, with the same
> risk backbone but opposite philosophies on how much the LLM gets to
> decide, and we're comparing them live, in public, with real data.

## B-roll cue

Good moment to cut to the `/compare` dashboard panel (three cards: Ours,
Paco, rookieriot) while this plays.
