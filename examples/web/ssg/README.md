# Experimental routed static site

From the repository root:

```sh
python -m nexo web build examples/web/ssg/app.nexo --static \
    --routes examples/web/ssg/routes.json -o dist
python -m nexo web preview dist
```

Open the printed loopback URL. Home and Guide are generated automatically.
Member URLs come from `routes.json`; add a concrete URL there before linking to
it. Load `/members/7/` directly or use the normal navigation links. The pages work
with JavaScript disabled. Preview also serves the generated not-found page with
HTTP 404.

Deploy the contents of `dist` at an origin root on a static host supporting
directory index files. Configure that host's error document to `404.html` with
HTTP 404. Deployment below a subdirectory, async data and hydration are outside
this example. See `docs/rfc/0003-web-routed-ssg.md` for the complete bounds.
