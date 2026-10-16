SPHINXBUILD ?= python -m sphinx
SOURCEDIR   = .
BUILDDIR    = _build

.PHONY: html clean

html:
	$(SPHINXBUILD) -W --keep-going -b html $(SOURCEDIR) $(BUILDDIR)/html

clean:
	rm -rf $(BUILDDIR)
