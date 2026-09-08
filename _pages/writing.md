---
layout: archive
title: "Writing"
permalink: /writing/
author_profile: true
---

{% include base_path %}

{% assign entries = site.writing | sort: "date" | reverse %}

{% if entries.size > 0 %}
  <!-- Deliberately not archive-single.html: that include labels every link
       "Download Paper", which is the wrong word for a short story. -->
  {% for post in entries %}
  <div class="list__item">
    <article class="archive__item">
      <h2 class="archive__item-title">
        <a href="{{ base_path }}{{ post.url }}" rel="permalink">{{ post.title }}</a>
      </h2>
      <p class="page__meta">
        {% if post.type %}{{ post.type }}, {% endif %}{{ post.date | date: "%Y" }}{% if post.venue %} &middot; <i>{{ post.venue }}</i>{% endif %}
      </p>
      {% if post.excerpt %}
      <p class="archive__item-excerpt">{{ post.excerpt | markdownify | remove: "<p>" | remove: "</p>" }}</p>
      {% endif %}
      {% if post.pdfurl %}
      <p><a href="{{ base_path }}{{ post.pdfurl }}">{{ post.pdflabel | default: "Read the PDF" }}</a></p>
      {% endif %}
    </article>
  </div>
  {% endfor %}
{% else %}
  <p>A place for the writing I do that isn't a paper — essays, notes, and
  whatever else doesn't belong on the <a href="{{ base_path }}/publications/">publications</a>
  page. Nothing here yet.</p>
{% endif %}
