---
title: "CV"
permalink: /cv/
redirect_to: /files/CV_Sebastian_Pohl.pdf
redirect_from:
  - /resume
sitemap: false
---

The CV lives in `files/CV_Sebastian_Pohl.pdf`, and the header's CV tab links to
it directly. This page exists only so that the older `/cv/` and `/resume` URLs
keep working; `redirect_to` (from the jekyll-redirect-from plugin) sends anyone
landing here on to the PDF. Nothing below the front matter is rendered.

To swap in a new CV, replace the PDF and update the two paths above plus the CV
entry in `_data/navigation.yml`. Keeping the filename stable is easier.
