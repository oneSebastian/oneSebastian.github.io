---
layout: archive
title: "Writing"
permalink: /writing/
author_profile: true
---

{% include base_path %}

{% assign entries = site.writing | sort: "date" | reverse %}

{% if entries.size > 0 %}
  <div class="wordwrap">Sometimes I also do some non-academic writing (usually in German). You can find some examples here.</div>

  <!-- Same reference-list shape as the Publications page: the entries have no
       page of their own (the writing collection sets output: false), so the
       title links straight at the PDF and the publishing details are printed
       here rather than one click away. -->
  <ol class="publication-list">
  {% for post in entries %}
    {% comment %} A title ending in '?' or '!' must not also gain a period. {% endcomment %}
    {% assign last_char = post.title | strip | slice: -1 %}
    {% if last_char == "." or last_char == "?" or last_char == "!" %}
      {% assign title_end = "" %}
    {% else %}
      {% assign title_end = "." %}
    {% endif %}
    <li class="publication-list__item">
      <span class="publication-list__title">{% if post.pdfurl %}<a href="{{ base_path }}{{ post.pdfurl }}">{{ post.title }}</a>{% else %}{{ post.title }}{% endif %}{{ title_end }}</span>
      <span class="publication-list__venue">{% if post.type %}{{ post.type }}{% if post.language %}, in {{ post.language }}{% endif %}. {% endif %}{% if post.venue %}In <i>{{ post.venue }}</i>{% if post.venue_note %} ({{ post.venue_note }}){% endif %}{% if post.publisher %}, {{ post.publisher }}{% endif %}, {% endif %}{{ post.date | date: "%Y" }}.</span>
    </li>
  {% endfor %}
  </ol>
{% else %}
  <p>A place for the writing I do that isn't a paper — essays, notes, and
  whatever else doesn't belong on the <a href="{{ base_path }}/publications/">publications</a>
  page. Nothing here yet.</p>
{% endif %}
