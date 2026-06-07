# Technical Report — Build Instructions

This directory contains a paper-standard LaTeX technical report for EBSearch
(EnhancedBilibiliSearch).

## Files

- `technical_report.tex` — the report source (IEEE conference format).

## Document class

The report uses `\documentclass[conference]{IEEEtran}`. `IEEEtran.cls` is **not
bundled here**; it ships with most TeX distributions in the
`texlive-publishers` package and is preinstalled on Overleaf (select the
"IEEE" template, or compile this file directly). If a local build reports
`IEEEtran.cls not found`, install the publishers collection, e.g.:

- TeX Live (Debian/Ubuntu): `sudo apt-get install texlive-publishers`
- TeX Live (tlmgr): `tlmgr install ieeetran`
- MacTeX: included in the full install.

## Package dependencies

All packages used are widely available in TeX Live / MiKTeX / Overleaf:

```
graphicx   booktabs   amsmath   listings
fontenc    inputenc   xcolor    url   hyperref
```

The system-architecture figure is a plain `verbatim` ASCII diagram (no TikZ),
so there is no risky package dependency. Tables use `booktabs`.

## Building

With a local TeX Live / MiKTeX install:

```sh
pdflatex technical_report.tex
pdflatex technical_report.tex   # second pass resolves references
```

The bibliography is an inline `thebibliography` environment, so no separate
BibTeX/Biber pass is required (no `refs.bib` is needed for this report).

On Overleaf: upload `technical_report.tex`, set the compiler to `pdfLaTeX`, and
compile.

## Notes

- Cost/latency figures are presented as **illustrative design estimates**, not
  measured benchmarks.
- Places marked `[Insert ... here]` indicate where a real screenshot or
  measured benchmark should be added later.
- No code, HTML, JS, or the repo README was modified to produce this report.
