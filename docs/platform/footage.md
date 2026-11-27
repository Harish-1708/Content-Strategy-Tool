# Footage inventory and cuts briefs

A **cuts** brief is an edit of footage the brand already has, not a new shoot. The tool builds the list of that footage itself; nobody lists clips by hand.

## Where the footage goes

In the Google Drive folder the Idea Bank already uses (the one in the `IDEA_BANK_FOLDER_ID` secret, shared with the tool's service account), inside the brand's own folder, in a folder called **Footage**:

    <Idea Bank folder> / <Brand name> / Footage /
        unboxing/IMG_0001.mov
        creators/Sarah/zip-demo.mp4
        ...

Subfolders are fine; their names and the file names become each clip's tags. The tool creates the Footage folder itself the first time it looks, so you can also just run Actions > **Footage** > scan and then open the link it prints.

## What the tool does

* **Footage > scan** (every Monday, and whenever you start it) lists everything under the folder, including subfolders, with Drive's own information for each file: length, size and shape of a video. **Nothing is downloaded**, so the folder can be as large as Drive allows. A clip whose length Drive has not read yet (a fresh upload) is listed without one and filled in by the next scan.
* The inventory is saved in `data/<brand>/extras/footage.json`: each clip has a stable number (`FT-xxxxxx`), its path and name, length, shape (vertical, square or horizontal), size, date, Drive link and tags.
* **Brief > kind: cuts** shows the writer the video clips (newest first) and asks for a shot list per concept: `clips: [{clip, start, end}]`, in playing order, parts inside each clip's own length. The checks (errors, so the brief needs review): a concept with no clips, a clip that is not in the inventory, a part that does not start before it ends, a part that runs past the clip's end. A warning when the parts add up to a length far from the one the format names.
* The Word brief prints each concept's shot list: clip number, file name, folder and the seconds used.
* If there is no inventory yet, a cuts brief stops before asking any model and says where the clips go.

## Not built

* The tool does not cut or render anything (that is Stage 7 layers 2 and 3). The shot list is the brief to the editor.
* It does not watch the clips: it cannot know what is in a clip beyond its name, folder and length. Name clips and folders for what they show; the writer chooses from those words.
