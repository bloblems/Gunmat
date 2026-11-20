# Gunmat

Turn a 3D model into a labeled parts drawing for a desk mat, a poster, or a manual. Bring your own STEP file (`.step` or `.stp`). A model with separate parts works best.

## Open the app

1. Download Gunmat using the green **Code** button, then **Download ZIP**. Unzip the folder.
2. Install **uv** once. It gets the right version of Python and the other tools Gunmat needs. [Install uv](https://docs.astral.sh/uv/getting-started/installation/)
3. On a Mac, double-click **start.command**. Or open a terminal in the unzipped folder and run this command on Mac, Windows, or Linux.

   ```sh
   uv run --no-project --python 3.12 --with-requirements requirements.txt -m cadraw
   ```

The first launch downloads the tools and can take a few minutes. Gunmat opens in your browser and runs on your computer. Keep the terminal open while you work. Press **Ctrl+C** there to stop.

## Make a drawing

1. Click **Open STEP…** and pick your file. Gunmat draws the parts and gives each one a number.
2. Move **Spread** to change the spacing. Drag a part to move it. Turn on **Parts list → Show on mat** if you want a list of names.
3. Click **PNG** for an image, **Export SVG** for a drawing that stays sharp at any size, or **Figma** for an SVG with editable text.

Your model and edits stay on your computer. Reopen the same file to keep working.

## License

You can use, change, and share Gunmat under the [MIT license](LICENSE). The bundled font reader also includes notices for [opentype.js](web/js/vendor/opentype.LICENSE) and [tiny-inflate](web/js/vendor/tiny-inflate.LICENSE).

## Reopen a drawing

Click **Open drawing…** and choose an SVG exported by Gunmat or Figma, or a Gunmat Figma plugin JSON file. Gunmat detects the file format and opens its existing artwork immediately.

Choose a layer from the list or click the artwork. Drag to move it, use arrow keys for small moves, edit live text, or change fill, stroke, and line weight. **Undo** reverses edits. **Export SVG** saves a version you can reopen again, **Figma** saves an SVG for Figma, and **PNG** saves an image.

For a drawing changed in Figma, export the frame as SVG first. Keep text as text in Figma's SVG export settings if you want to edit the words in Gunmat. Outlined letters remain editable vector shapes. Older plugin JSON files are reconstructed from their saved pieces and may need text spacing adjustments. New plugin files include the complete SVG.

Drawing edits stay in the open page until you export them. Save your SVG before closing the page or opening another file.
