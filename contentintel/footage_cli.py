"""`footage`: the footage inventory, built from the brand's Footage folder in Google Drive (see footage.py for where the clips go). scan = list the folder and save the inventory; status = what is saved."""
from . import footage as FT
from .brands import list_brand_slugs, load_brand


def add_parsers(sub, common):
    p = sub.add_parser("footage", help="the footage inventory, built from <Idea Bank folder>/<brand>/Footage in Google Drive: scan it, or show what is saved")
    common(p)
    p.add_argument("--action", choices=["scan", "status"], default="scan")
    p.add_argument("--brand", default="all")


def run_footage(args, store, now, drive=None, missing=""):
    slugs = list_brand_slugs(args.brands_dir) if args.brand == "all" else [args.brand]
    worst = 0
    for slug in slugs:
        if args.action == "status":
            data = FT.load(store, slug)
            if not data.get("clips"):
                print(f"{slug}: no footage inventory yet: put the clips in <Idea Bank folder>/{load_brand(slug, args.brands_dir).name}/Footage in Google Drive and run Footage > scan")
                continue
            print(f"{slug}: {data['count']} clip(s) ({data['videos']} video, {data['total_seconds']:g} s in all, {data['without_length']} without a length yet), scanned {str(data['updated'])[:10]}; folder {data['folder_url']}")
            continue
        if drive is None:
            print(f"{slug}: cannot scan: the {missing or 'Google Drive'} secret is not set in this workflow")
            worst = 1
            continue
        brand = load_brand(slug, args.brands_dir)
        folder = drive.footage_folder(brand.name)                                          # made now when it is not there yet
        clips, ignored = FT.build_inventory(drive.list_tree(folder))
        FT.save(store, slug, clips, ignored, folder, now)
        videos = [c for c in clips if c["kind"] == "video"]
        print(f"{slug}: scanned {brand.name}/Footage: {len(clips)} clip(s) ({len(videos)} video, {sum(c['seconds'] or 0 for c in videos):g} s in all), {sum(1 for c in videos if not c['seconds'])} video(s) with no length read by Drive yet, "
              f"{ignored} other file(s) ignored" + ("" if clips else f". The folder is empty: put the clips in https://drive.google.com/drive/folders/{folder} (Footage inside the {brand.name} folder of the Idea Bank Drive folder) and scan again"))
    return worst
