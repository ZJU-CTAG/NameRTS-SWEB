## sympy
```bash
git clone https://github.com/sympy/sympy.git
cd sympy
conda create -n RTSTest_SY python==3.13
conda activate RTSTest_SY
python -m pip install --upgrade pip setuptools
pip install -r requirements-dev.txt
```

## sklearn
```bash
git clone https://github.com/scikit-learn/scikit-learn.git
cd scikit-learn
conda create -n RTSTest_SC python==3.13
conda activate RTSTest_SC
apt-get install build-essential python3-dev python3-pip
pip3 install cython
pip install meson-python
apt install ninja-build
pip install -e ".[build,install,benchmark,docs,examples,tests,maintenance]"
pip3 install --editable .     --verbose --no-build-isolation     --config-settings editable-verbose=true
apt-get install cython3 python3-numpy python3-scipy
pip install pytest-xdist

```

## mpl
```bash
git clone https://github.com/matplotlib/matplotlib.git
cd matplotlib
apt update
conda create -n RTSTest_MA python==3.13
conda activate RTSTest_MA
apt install build-essential
apt-get install -yy --no-install-recommends \
              ccache \
              cm-super \
              dvipng \
              fonts-freefont-otf \
              fonts-noto-cjk \
              fonts-wqy-zenhei \
              gdb \
              gir1.2-gtk-3.0 \
              graphviz \
              inkscape \
              language-pack-de \
              lcov \
              libcairo2 \
              libcairo2-dev \
              libffi-dev \
              libgeos-dev \
              libnotify4 \
              libsdl2-2.0-0 \
              libxkbcommon-x11-0 \
              libxcb-cursor0 \
              libxcb-icccm4 \
              libxcb-image0 \
              libxcb-keysyms1 \
              libxcb-randr0 \
              libxcb-render-util0 \
              libxcb-xinerama0 \
              lmodern \
              ninja-build \
              pkg-config \
              qtbase5-dev \
              texlive-fonts-recommended \
              texlive-latex-base \
              texlive-latex-extra \
              texlive-latex-recommended \
              texlive-luatex \
              texlive-pictures \
              texlive-xetex
pip install meson-python
pip install setuptools_scm
pip install  pybind11
pip install pytest
pip install pytest-xdist
python -m pip install --verbose --no-build-isolation --editable ".[dev]"
```

## dask
```bash
git clone https://github.com/dask/dask.git
cd dsak
conda create -n RTSTest_DAS python==3.13
pip install -e ".[complete,test]"
```

## xarray
```bash
git clone https://github.com/pydata/xarray.git
cd xarray
conda create -n RTSTest_XA python==3.13
conda activate RTSTest_XA

# need to modify the env name in environment.yml as RTSTest_XA!
conda env update -f ci/requirements/environment.yml

pip install -e .
```

## sphinx
```bash
git clone https://github.com/sphinx-doc/sphinx.git
cd sphinx
conda create -n RTSTest_SPH python=3.13
conda activate RTSTest_SPH
pip install -e .
pip install pytest pytest-xdist cython defusedxml setuptools typing_extensions
```

## pylint
```bash
git clone https://github.com/pylint-dev/pylint.git
cd pylint
conda create -n RTSTest_PYL python==3.13
apt-get update
apt-get install libenchant-2-2
python -m pip install --upgrade pip setuptools wheel
pip install -e ".[all]"
pip install pytest GitPython pytest_benchmark pyenchant
```

## seaborn
```bash
git clone https://github.com/mwaskom/seaborn.git
cd seaborn
conda create -n RTSTest_SE python==3.12
conda activate RTSTest_SE
pip install --upgrade pip wheel
pip install pyparsing
pip install .[dev,stats]
pytest -n auto --cov=seaborn --cov=tests --cov-config=setup.cfg tests
```

## pvlib
```bash
git clone https://github.com/pvlib/pvlib-python.git
cd pvlib-python
conda create -n RTSTest_PVL python=3.13
conda activate RTSTest_PVL
pip install -U pip setuptools setuptools-scm wheel
pip install -e .[all]
pip install pytest==8.4.2
```

## loguru
```bash
git clone https://github.com/Delgan/loguru.git
cd loguru
conda create -n RTSTest_LOG python=3.13
conda activate RTSTest_LOG
python -m pip install -U pip wheel setuptools
pip install -e ".[dev]"
pip install -U freezegun
```

