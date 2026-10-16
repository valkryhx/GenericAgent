import re, json

p = "liu_guoliang_introduction.html"
html = open(p, encoding="utf-8").read()

checks = {}
checks["doctype"] = html.strip().startswith("<!DOCTYPE html>")
checks["closing_html"] = html.rstrip().endswith("</html>")
checks["no_placeholder"] = "country" not in html and "d4a" not in html
for tag in ["section","div","ol","ul","table","header","footer","nav"]:
    o = len(re.findall(r"<%s[\s>]" % tag, html))
    c = len(re.findall(r"</%s>" % tag, html))
    checks["tag_"+tag] = (o, c, o == c)

links = re.findall(r'href="(https?://[^"]+)"', html)
checks["link_count"] = len(links)
checks["unique_links"] = len(set(links))
checks["source_items"] = len(re.findall(r'<li>\s*<a href', html))
checks["cred_badges"] = len(re.findall(r'class="cred ', html))
for k in ["overview","career","influence","sources"]:
    checks["sec_"+k] = ('id="%s"' % k) in html

print(json.dumps(checks, ensure_ascii=False, indent=1))

src_urls = set()
for f in ["research_liu_guoliang_sports.json","research_liu_guoliang_influence.json"]:
    d = json.load(open(f, encoding="utf-8"))
    for s in d["sources"]:
        src_urls.add(s["url"])
html_links = set(links)
missing = src_urls - html_links
print("\nJSON source urls:", len(src_urls), "| HTML unique links:", len(html_links))
print("Missing:", len(missing))
for m in sorted(missing): print("  MISSING:", m)
print("Extra:", len(html_links - src_urls))
for e in sorted(html_links - src_urls): print("  EXTRA:", e)
