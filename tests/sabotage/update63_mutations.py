"""Deliberate-bug checks for Update 63 (the benchmark page finder: its key, its status line, its own mode, the page id). Same format as run_observatory_sabotage.MUTATIONS."""
U63 = [
    ('u63-no-serper-check', 'contentintel/cli.py', '    if serper is None:\n        return None, "no Serper search is available', '    if False:\n        return None, "no Serper search is available', 'a finder is built without a search'),
    ('u63-no-ai-check', 'contentintel/cli.py', '    if claude is None or not hasattr(claude, "json_task"):\n        return None, "no AI client', '    if False:\n        return None, "no AI client', 'a finder is built without an AI client'),
    ('u63-find-limit', 'contentintel/cli.py', 'cfg["ads"].get("benchmark_pages"), finder, now, 10)', 'cfg["ads"].get("benchmark_pages"), finder, now, 0)', 'the benchmarks mode looks for nothing'),
    ('u63-find-not-saved', 'contentintel/cli.py', '                if lines:\n                    store.write_benchmarks(slug, state)\n            print(f"--- {slug}: benchmark pages looked for ---")', '                if False:\n                    store.write_benchmarks(slug, state)\n            print(f"--- {slug}: benchmark pages looked for ---")', 'what the finder finds is not saved'),
    ('u63-find-warning', 'contentintel/cli.py', '        if finder is None:\n            print(f"::warning::The benchmark page finder is off', '        if False:\n            print(f"::warning::The benchmark page finder is off', 'an off finder does not warn'),
    ('u63-status-line', 'contentintel/cli.py', '    if not args.find_pages and not args.dry_run and not args.seeds', '    if False and not args.dry_run and not args.seeds', 'a real collect does not say whether the finder is on'),
    ('u63-extra-secret', 'contentintel/cli.py', 'EXTRA_SECRETS = ("ANTHROPIC_API_KEY", "APIFY_TOKEN", "SERPER_API_KEY")', 'EXTRA_SECRETS = ("ANTHROPIC_API_KEY", "APIFY_TOKEN")', "the log does not list Serper's key"),
    ('u63-wf-key', '.github/workflows/collect.yml', '          SERPER_API_KEY: ${{ secrets.SERPER_API_KEY }}\n', '', 'the collect run gets no Serper key'),
    ('u63-wf-mode', '.github/workflows/collect.yml', '            benchmarks)         args=(--find-pages) ;;\n', '', 'the benchmarks mode is unknown'),
    ('u63-wf-guard', '.github/workflows/collect.yml', '[ "$MODE" != "seeds" ] && [ "$MODE" != "benchmarks" ] && [ "$MODE" != "rest" ]', '[ "$MODE" != "seeds" ] && [ "$MODE" != "rest" ]', 'the benchmarks mode stops for want of a ticked source'),
    ('u63-mp-id', 'contentintel/main_page.py', '    page_id = chosen.get("id") or read_id(chosen["url"])', '    page_id = read_id(chosen["url"])', 'an id already in the address is not used'),
    ('u63-mp-people', 'contentintel/main_page.py', '        people = FB_PEOPLE.match(url.strip())', '        people = None', 'addresses holding an id are not recognised'),
    ('u63-mp-basic', 'contentintel/main_page.py', ', f"https://mbasic.facebook.com/{slug}"] if slug else [url])', '] if slug else [url])', 'the basic address is never tried'),
    ('u63-mp-why', 'contentintel/main_page.py', '        read_id.why = "tried " + "; ".join(tried)', '        read_id.why = ""', 'a failed page read gives no reason'),
    ('u63-mp-renormalise', 'contentintel/main_page.py', '            found += list(search(query.format(name=name)) or [])', '            found += candidates_from(search(query.format(name=name)))', 'candidates are normalised twice and lose their slug'),
    ('u63-mp-pattern', 'contentintel/main_page.py', 'r\'"delegate_page_id"\\s*:\\s*"?(\\d{6,})\', ', '', 'the delegate_page_id form is not read')
]
