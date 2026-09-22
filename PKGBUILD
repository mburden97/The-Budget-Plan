# Maintainer: mburden97 <113633028+mburden97@users.noreply.github.com>
#
# Builds the checkout this file sits in, so there is nothing to download:
#     makepkg -si
# Then run `budget`, or start "The Budget Plan" from the applications menu. The vault lives
# in ${XDG_DATA_HOME:-~/.local/share}/thebudgetplan/data.

pkgname=the-budget-plan
pkgver=0.1.0
pkgrel=1
pkgdesc='Personal, local-only, encrypted budget, loan, brokerage and net worth tracker'
arch=('any')
url='https://github.com/mburden97/The-Budget-Plan'
license=('GPL-3.0-only')
depends=('python' 'python-flask' 'python-waitress' 'python-cryptography')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
checkdepends=('python-pytest')
options=('!debug')
source=()

build() {
    cd "$startdir"
    python -m build --wheel --no-isolation
}

check() {
    cd "$startdir"
    PYTHONPATH="$startdir/src" python -m pytest
}

package() {
    cd "$startdir"
    python -m installer --destdir="$pkgdir" dist/*.whl
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
    install -Dm644 SECURITY.md "$pkgdir/usr/share/doc/$pkgname/SECURITY.md"
    install -Dm644 contrib/the-budget-plan.desktop \
        "$pkgdir/usr/share/applications/the-budget-plan.desktop"
}
