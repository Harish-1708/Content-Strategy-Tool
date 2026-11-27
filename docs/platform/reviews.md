# Customer reviews and word-for-word quotes

A brief may quote a customer only **verbatim**: never invented, never joined from two reviews, never polished (the DudeRobe rule: "Customer quotes must be verbatim from the Judge.me export; never invent or combine"). The tool makes that exact.

## Where the reviews come from (Actions > Reviews, fully automatic)

**collect** is the default and needs nothing from you. It runs by itself every Sunday:

1. It reads each product page of the brand's own shop (the products in the catalogue) and finds the shop and the product number in Judge.me's own widget markup.
2. It then reads Judge.me's public pages of reviews for that product, page by page, newest first, until an empty page: the **first reading of a product takes its whole history**.
3. Progress is saved after every product, so a run that reaches its time limit (150 minutes) is simply continued by the next Sunday's run until every product is read in full.
4. After that a product is only **topped up**: the newest pages, until one holds nothing new, once its last top-up is 28 days old. A run with nothing due says "up to date" and asks nothing.
5. Reading is paced (one request a second by default). If Judge.me's page address refuses three products in a row the run stops and says so; the first page of each product's reviews drawn into the page is still kept, and the next run retries.

**import** is optional and stays available: Judge.me > Reviews > Export, upload the CSV to `imports/<brand>-judgeme-reviews.csv`, then run Reviews > import. **status** says how many reviews are stored, how many products were read in full, and from which source.

Saved in `data/<brand>/extras/reviews.json` (the reviews and, beside them, each product's collection progress). **No email address or IP address is ever read or kept**, and nothing is written to a shop or to Judge.me. The same review is never stored twice.

## How a brief uses them

* The writer is shown up to 8 reviews it may quote: five star, one or two sentences long, the brief's own products first, newest first. It must copy a quote exactly and name the review (`quote: {review_id, text}`).
* **Checks (errors: the brief needs review).** A concept's quote must be found, character for character, inside the one review it names (spacing and curly or straight quotation marks are ignored; case, words and punctuation are not). Any passage of four words or more that a concept puts in double quotation marks must be found inside ONE stored review. A quote that is edited, shortened inside a sentence, combined from two reviews or invented is flagged; **so is any quote when no review is stored for the brand** ("collect the reviews first").
* The Word brief shows the quote with its attribution: reviewer, date, source, stars and review number.
* A revision is told exactly which quote to fix.

## Not built / not proven

* **The collection is not proven live.** The build sandbox cannot reach Judge.me or the shops, so the widget markup and Judge.me's page address were written from Judge.me's own class names and a fixture, and tested against a simulated Judge.me with real pagination. The first Reviews run on GitHub shows the truth: its summary says, per product, how many pages were read, whether the product was read in full, and exactly what was missing (no widget found, a refused page address, a time limit). If Judge.me ever changes its markup or address, the run says so; the optional export import then still gives every review.
* "Use 3,200+ five-star reviews; do not use 3,700+ without confirmation" (DudeRobe) stays a person's check: the tool does not count claims about review totals.
