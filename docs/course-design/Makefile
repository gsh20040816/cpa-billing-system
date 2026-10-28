# Build the course-design report (XeLaTeX + Graphviz).
#   make            -> build/report.pdf
#   make OUT=/path  -> also copy the PDF there
PYTHON ?= python3
FIGURES := $(patsubst figures/%.dot,build/figures/%.pdf,$(wildcard figures/*.dot)) build/figures/schema.pdf

build/report.pdf: report.tex $(wildcard chapters/*.tex) generated/data-dictionary.tex $(FIGURES)
	latexmk -xelatex -interaction=nonstopmode -halt-on-error -outdir=build report.tex
ifdef OUT
	cp build/report.pdf "$(OUT)"
endif

generated/data-dictionary.tex generated/triggers.tex generated/schema.dot: tools/gen_schema_tex.py ../../cpa_billing/models.py ../../cpa_billing/integrity.py
	$(PYTHON) tools/gen_schema_tex.py generated

build/figures/schema.pdf: generated/schema.dot
	@mkdir -p build/figures
	dot -Tpdf $< -o $@

build/figures/%.pdf: figures/%.dot
	@mkdir -p build/figures
	dot -Tpdf $< -o $@

clean:
	rm -rf build

.PHONY: clean
