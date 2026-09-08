# Providence

A geospatial intelligence platform for Barcelona, built on public data.

Live: [franciscopecora.github.io/Barcelona-crime-intel-map](https://franciscopecora.github.io/Barcelona-crime-intel-map)

Providence fuses eleven municipal, national and satellite data sources into a single
analytical surface, and applies one rule throughout: every claim states its method,
its sample size, and what would falsify it. Where the data does not support a
conclusion, the interface says so rather than filling the gap.

---

## The finding worth reading first

**Road works are associated with a 13x increase in congestion frequency on affected
street sections.**

Barcelona publishes two independent feeds that were never designed to be read
together: a live traffic-state feed covering 534 street sections, and a register of
active road works with locations and date ranges. Neither says anything about the
other.

Joining them across a rolling twelve-month window produces a natural experiment. For
each street section, congestion frequency can be measured during periods when road
works were active on it, and compared against the same section's congestion frequency
when they were not. The section is its own control, which removes the obvious
confounder that busy streets are both more congested and more likely to be dug up.

Result on the strongest-evidenced section (Passeig Maragall, section 244):

| | Congestion frequency |
|---|---|
| During active road works | 35.5% |
| No road works | 2.7% |
| **Lift** | **13.22x** |
| Observations | 12,063 |

Across 457 sections with sufficient data, 249 showed a measurable lift.

**What this does not show.** It is an association, not a causal estimate. Road works
are not randomly assigned, so a section scheduled for works may already have been
degrading. Sections with a very rare baseline produce inflated ratios and are flagged
as such in the interface rather than being reported as headline numbers. The
twelve-month window is fixed and the analysis has not been repeated across
independent windows.

The pipeline is in `compute_roadwork_evidence.py`. It runs against live public
endpoints and writes `roadwork_evidence.json`. Re-run it and you should reproduce
these numbers.

---

## Why it exists

Barcelona publishes a large amount of open data. It is scattered across incompatible
grains: crime by district, traffic by street section, demographics by census section,
construction by decade bucket. Each dataset answers a narrow question well. Almost
nothing answers a question that spans two of them.

Providence exists to make cross-domain questions askable, and to be honest about how
weak most of the answers are.

---

## What it does

**Fusion.** Ten domains reduced to a common district grain and cross-referenced
pairwise: crime, population density, foreign residents, traffic congestion, road
works, live events, transit coverage, income, elderly share, youth share. The
ontology layer bridges grains so a street-section measurement can be related to a
district-level statistic without pretending they were measured the same way.

**Analysis.** Pearson correlations across domain pairs with 95% confidence intervals.
Any correlation whose interval crosses zero is visually demoted and labelled as not
significant. With n=10 districts, most of them are. This is stated rather than hidden.

The one association that survives with reasonable strength is crime against income at
r = -0.51: lower-income districts see more recorded crime. Income is a stronger single
correlate than population density, which is a real finding, but income and density are
themselves correlated and ten districts cannot support the multivariate analysis that
would separate them.

**Change Profile.** Fuses satellite imagery with multi-year income records per
neighbourhood. For each area it shows the true-colour satellite composite before and
after, the income change over the same period, and that change measured against the
citywide average. Areas rising in line with the city trend are demoted; only areas
meaningfully above trend are flagged. Every figure links back to its source file.

**Live layers.** Traffic state, road works, infrastructure, human terrain, aircraft,
RF. Feeds fail quietly and fall back to last-known-good rather than blanking the map.

---

## The satellite work, including what failed

The goal was per-neighbourhood change detection: identify where Barcelona had been
physically redeveloped, then check that against the cadastral record to find
construction that was never registered.

It did not work, and the reason is worth documenting.

Sentinel-2 is 10m per pixel. Four detector variants were built and tested against
known ground truth:

1. NDBI differencing between single scenes
2. Median composites over six-month windows to cancel seasonal and atmospheric noise
3. A multi-index transition test requiring vegetation loss and a genuine
   not-built-to-built transition
4. Total multi-band spectral change magnitude, ranking areas by how much they changed
   rather than attempting to classify the change

Each failed differently. The clearest diagnostic came from variant 4. La Marina del
Prat Vermell, the site of Barcelona's largest active redevelopment, ranked tenth of
fifteen. Sant Antoni, a stable and fully built Eixample neighbourhood, ranked first.

The cause is that the noise floor is the same magnitude as the signal. In dense
Barcelona the real change was industrial-bare to built, which is a small spectral
shift, while a park's seasonal vegetation swing is a large one. No threshold recovers
a signal smaller than the noise it sits in. This is a sensor limitation, not a tuning
problem, and higher-resolution imagery is the only fix.

**What Sentinel-2 does support** is visual before-and-after comparison. Median
composites over a full year produce clean, cloud-free true-colour imagery. Placed side
by side, redevelopment at La Marina is plainly visible: a large industrial roof in
2021 replaced by a grid of new structures by 2026. The Eixample pair over the same
period looks nearly identical, which is the correct result.

So the platform shows the imagery and lets the reader judge, rather than reporting a
change statistic the sensor cannot support. Pipelines for both the failed detectors
and the working imagery tool are included.

---

## Architecture

Single-file frontend, vanilla JavaScript and Leaflet, no build step. Heavy computation
runs offline in Python and commits small JSON artefacts the browser reads. Raster
imagery and twelve months of traffic history do not belong in a browser.

```
Python pipelines (run locally)          Frontend (static)
────────────────────────────            ──────────────────
compute_roadwork_evidence.py    ──▶     roadwork_evidence.json
change_profile_engine.py        ──▶     change_profiles.json + imagery
oracle_beforeafter.py           ──▶     satellite composites
```

The change-profile engine separates a city-agnostic engine from a swappable config.
The engine handles areas, physical observation, social metrics, divergence and
confidence. Barcelona-specific loaders sit behind data contracts in the config. A
different city means writing a new config, not touching the engine.

One note on the pipelines: Barcelona's open-data portal blocks cloud and CI ranges,
so they must run from a residential connection. Scheduled CI is not an option.

**Sources.** Ajuntament de Barcelona open data, Generalitat de Catalunya, TMB,
INE via the Atles de Renda, ESA Copernicus Sentinel-2. CORS is handled by a
Cloudflare Worker proxy.

---

## Reproducing it

```bash
pip3 install requests numpy pillow pyproj shapely

python3 compute_roadwork_evidence.py     # 12-month traffic and road-works join
python3 change_profile_engine.py --all   # satellite plus income per neighbourhood
python3 oracle_beforeafter.py            # before and after composites
```

Copernicus pipelines need `oracle_credentials.json` containing a Copernicus Data
Space client ID and secret.

Everything here derives from public data. Nothing needs to be taken on trust, because
all of it can be recomputed.

---

## Honest limits

- **n=10 districts.** Most correlations cannot reach significance and are labelled
  accordingly. District-level analysis is a screening tool, not evidence.
- **Recorded crime is not crime.** It measures reporting and enforcement as much as
  offences, and reporting rates differ systematically across districts.
- **Grain mismatch.** Bridging street sections, districts and census sections loses
  information at every join.
- **10m satellite resolution** supports visual comparison, not automated per-area
  change statistics, for the reasons above.
- **Income is an area average.** It cannot distinguish existing residents earning more
  from higher-earning residents replacing them, which is exactly what would be needed
  to say anything real about displacement.

---

Built by Francisco Pecora.
