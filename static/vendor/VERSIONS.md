# Bundled JavaScript libraries

These files are served by the app itself, so the browser never loads anything from a CDN.
They were taken from the official npm packages (registry.npmjs.org); each tarball's sha512
was checked against the registry before the file was copied here. All are MIT licensed;
the licence texts are in `licenses/`.

| File | Package | Version | SHA-256 of this file |
| --- | --- | --- | --- |
| cytoscape.min.js | cytoscape | 3.34.3 | 5f3b5b529546d5af1fc5628590af033b74511a5b6f789f5f4682845863228b91 |
| cytoscape-dagre.min.js | cytoscape-dagre (includes dagre) | 4.0.1 | ec671a41d2bfcac580352e2997797793949e3489cecaf347fc48670d4bd456b0 |
| cytoscape-fcose.js | cytoscape-fcose | 2.2.0 | 4b1cab218d74996aa59cd8473f9239cc6398b8c1774d84d7e59ad9a68959cb57 |
| cose-base.js | cose-base | 2.2.0 | 7cae9509bd36235a63a85e71c8d9fa2cd0bc1d0c1ecc5b5a737976f39d040ddf |
| layout-base.js | layout-base | 2.0.1 | ec15ab5df9af3f20708f4faab994accf91cda71848cd5bb10a23432cc50b6745 |
| chart.umd.min.js | chart.js (includes @kurkle/color) | 4.5.1 | 48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a |

Load order in `index.html`: cytoscape, layout-base, cose-base, cytoscape-fcose, cytoscape-dagre, chart.umd.min.

`chart.umd.min.js` is `dist/chart.umd.min.js` of the chart.js package, unchanged (added in session 2 for the
histograms of the Profile tab). It ends with a source-map comment; the map is not bundled, so a browser's
developer tools may ask this app for `chart.umd.min.js.map` and get a 404. Nothing is requested elsewhere.

To update one: download the package tarball from `https://registry.npmjs.org/<package>/-/<package>-<version>.tgz`,
verify its integrity, copy the file listed above from it, and update this table
(`tests/test_security.py` checks the hashes).
