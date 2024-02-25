# HTML music library generator script

Python script to create an HTML audio library file listing in a flat page all the media files under a given folder (current folder by default).

## Usage

Generate an HTML library with all audio files under the current folder. The default file name is `mu.html` in the target folder:

```bash
music-gallery-creator.py
```

Generate a library file for a custom folder including video files, skipping folder names containing `testing` and `unsorted` (the index will be created under `~/Music/music.html`):

```bash
music-gallery-creator.py ~/Music --videos -o music.html -i testing unsorted
```

## Screenshots

![](https://telegra.ph/file/9a8783c925cd855666946.png)

## Command line flags

For commmand line usage run `music-gallery-creator.py -h`:

```bash            
usage: music-gallery-creator.py [-h] [--output-file output_file] [--videos] [--ignored [ignore ...]] [--verbose] [gallery_root]

Music gallery Generator

positional arguments:
  gallery_root          Gallery root, by default current folder

options:
  -h, --help            show this help message and exit
  --output-file output_file, -o output_file
                        Output filename
  --videos              Include videos
  --ignored [ignore ...], -i [ignore ...]
                        Custom ignored path segments. Accepts multiple segments, e.g. -i junk1 junk2 junk3 "[]"
  --verbose, -v         Verbose output
```
