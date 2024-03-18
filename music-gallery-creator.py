#!/usr/bin/env python3

import itertools
import html
import pathlib
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# HTML gallery output file
OUTPUT_FILE_NAME = "mu.html"

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
VIDEO_EXTENSIONS = (".mp4", ".mkv", ".3gp", ".mov", ".mkv", ".ogv", ".mpg", ".mpeg")
AUDIO_EXTENSIONS = (".mp3", ".webm", ".ogg", ".wav", ".flac", ".m4a", ".aac")

ASSET_EXTENSIONS = IMAGE_EXTENSIONS + AUDIO_EXTENSIONS

# literal lowercase path segments to ignore (matches intermediate folders too)
IGNORED_PATHS = [
    '.DS_Store',
    'site-packages',
    'assets/icons',
    'renditions',
    '_thumb',
    '.config',
    '.thumb',
    '/tests/',
    'cache/',
    '/Library/Application/',
]

total_images = 0
total_audios = 0
total_videos = 0
total_albums = 0

def get_created_time(file_path: Path) -> datetime:
    file_stat = file_path.stat()
    try:
        return datetime.fromtimestamp(file_stat.st_birthtime)
    except AttributeError:
        # We are probably on Linux. No way to get the creation date, only the last modification date.
        return datetime.fromtimestamp(file_stat.st_mtime)


UNITS_MAPPING = [
    (1024 ** 5, 'P'),
    (1024 ** 4, 'T'),
    (1024 ** 3, 'G'),
    (1024 ** 2, 'M'),
    (1024 ** 1, 'K'),
    (1024 ** 0, (' byte', ' bytes')),
]


def pretty_size(bytes, units=UNITS_MAPPING) -> str:
    """Human-readable file sizes.

    ripped from https://pypi.python.org/pypi/hurry.filesize/
    """
    for factor, suffix in units:
        if bytes >= factor:
            break
    amount = int(bytes / factor)

    if isinstance(suffix, tuple):
        singular, multiple = suffix
        if amount == 1:
            suffix = singular
        else:
            suffix = multiple
    return str(amount) + suffix

def funny_walk(start_dir, extensions, ignored_path_fragments):
    root_path = Path(start_dir)

    for curpath in itertools.chain([root_path], root_path.rglob('*')):

        if len([ignored for ignored in ignored_path_fragments if ignored.lower() in str(curpath.absolute()).lower()]):
            continue

        try:
            if curpath.is_dir():
                # dirs = [subdir for subdir in curpath.iterdir() if subdir.is_dir()]
                files = [file for file in curpath.iterdir() if file.is_file() and not file.name.startswith('._')]
                audios = [f for f in files if f.suffix.lower() in AUDIO_EXTENSIONS]
                images = [i for i in files if i.suffix.lower() in IMAGE_EXTENSIONS]
                videos = [v for v in files if v.suffix.lower() in VIDEO_EXTENSIONS]

                yield curpath, audios, images, videos
        except Exception as e:
            print(str(e))


def collect_media_assets(args) -> list[str]:
    global total_images
    global total_audios
    global total_videos
    global total_albums
    top_of_the_tree: Path = args.gallery_root.absolute()

    chunks: list[str] = []

    ignored_segments = IGNORED_PATHS + args.ignored
    for curdir, audios, images, videos in funny_walk(top_of_the_tree, ASSET_EXTENSIONS, ignored_segments):

        try:
            if len(audios):
                audios.sort(key=lambda p: p.name)
                total_albums += 1
                relative_path_to_album = str(curdir.relative_to(top_of_the_tree))
                escaped_path_to_album_dir = quote(relative_path_to_album)

                chunks.append(f'''<div class="item" title="{html.escape(str(curdir))}">''')
                chunks.append(f'''<h2><a href="{escaped_path_to_album_dir}" target="_blank">{html.escape(curdir.name)}</a></h2>''')

                if len(images):
                    cover = None

                    for candidate in ['cover', 'folder', 'front', 'album', 'card', 'thumb', 'back']:
                        matching_images = [img for img in images if candidate in img.stem.lower()]

                        if matching_images:
                            cover = matching_images[0]
                            break

                    # # generator to find the first matching image
                    # cover = next((img for img in images if any(candidate in img.stem.lower() for candidate in candidates)), None)

                    if not cover:
                        cover = images[0]

                    if cover:
                        cover_image = str(cover.relative_to(top_of_the_tree))
                        chunks.append(f"""<a href="{escaped_path_to_album_dir}" target="_blank" title="{html.escape(relative_path_to_album)}">
  <img src="{quote(cover_image)}" loading="lazy">
</a>""")
                        total_images += 1

                chunks.append('<div class="list">')
                for audio_path in audios:
                    size: int = audio_path.stat().st_size
                    size_pretty: str = pretty_size(size)

                    created_date = get_created_time(audio_path)

                    created_date_formatted = created_date.strftime("%Y-%m-%d %H:%M:%S")
                    relative_path = str(audio_path.relative_to(top_of_the_tree))
                    escaped_path = quote(relative_path)

                    chunks.append(f"""<p><a href="{escaped_path}" target="_blank" title="{relative_path} size: {size_pretty}; created: {created_date_formatted}">
&#x25B6;&#xFE0F; {html.escape(audio_path.name)} <span class="meta">({size_pretty})</span></a></p>""")
                    total_audios += 1

                    if args.verbose:
                        print(f'{audio_path}')
                    elif total_audios % 42:
                        print(f'audios:     {total_audios}              ', end='\r')
                chunks.append("""</div></div>""")
            if args.videos and len(videos):
                for video_path in videos:
                    total_videos += 1
                    vid_size: int = video_path.stat().st_size
                    vid_size_pretty: str = pretty_size(vid_size)

                    vid_created_date = get_created_time(video_path)

                    vid_created_date_formatted = vid_created_date.strftime("%Y-%m-%d %H:%M:%S")
                    vid_relative_path = str(video_path.relative_to(top_of_the_tree))
                    vid_escaped_path = quote(vid_relative_path)
                    chunks.append(f"""<a class="item" href="{vid_escaped_path}" target="_blank" title="{vid_relative_path} size: {vid_size_pretty}; created: {vid_created_date_formatted}">
                        <video preload="metadata" controls><source src="{vid_escaped_path}#t=0.1"></video>{video_path.name}
                        <span class="meta">({vid_size_pretty})</span>
                    </a>""")
        except Exception as e:
            print(f'💥 ERROR: {str(e)}')
    return chunks


def generate_gallery_html(html_data, args):
    output_path = args.gallery_root.absolute() / args.output_file

    with output_path.open(mode="w", encoding="utf-8") as fout:
        fout.write(r'''<html>
    <head>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
        * {
            font-family: system-ui, sans-serif;
        }
        body {
            background: #fafafa;
        }
        h1 {
            padding-top: 2em;
            font-size: 1.36em;
            color: #555555;
            text-align: center;
            font-family: Monospace;
            padding: 2em 2em 0.5em;
            font-size: 14pt;
            margin: 0;
        }
        .item > h2 {
            color: #4b4b4b;
            text-align: center;
            background-color: #dfdfde;
            font-family: "Arial Narrow", "Din condensed";
            padding: 0.6em 0.5em;
            font-size: 11pt;
            border-radius: 16px 16px 16px 16px;
        }
        h2 a:hover {
            color: white;
        }

        h2 a:visited {
            color: #4b4b4b;
        }

        h2 a:visited:hover {
            color: white;
        }        
        #maine {
            display: grid; 
            grid-template-columns: repeat(auto-fill, minmax(20em, 1fr)); 
            gap: 2px;
        }
        ::-webkit-scrollbar-corner { background: rgba(0,0,0,0.5); }

        * {
            scrollbar-width: thin;
            scrollbar-color: #ebebeb #f6f6f6;
        }

        /* Works on Chrome, Edge, and Safari */
        *::-webkit-scrollbar {
            width: 1px;
            height: 1px;
        }

        *::-webkit-scrollbar-track {
            background: #f6f6f6;
        }

        *::-webkit-scrollbar-thumb {
            background-color: #c5c5c5;
            border-radius: 5px;
            border: 3px solid #f6f6f6;
        }        
        .item {
            display: flex;
            flex-direction: column;
            align-items: left;
            justify-content: left;

            max-height: 650px;
            border-radius: 4px;
            background-color: white;
            padding: 10px;
            margin: 6px;
            border-width: 0;
            border-style: solid;
            border-color: white;
            box-shadow: 0px 2px 1px -1px rgba(0, 0, 0, .2), 0px 1px 1px 0px rgba(0, 0, 0, .14), 0px 1px 3px 0px rgba(0, 0, 0, .12);
        }
        .item img {
            width: 100%;
            height: auto;
            display: table-cell;
        }
        .item video {
            width: auto;
            height: auto;
            display: table-cell;
        }

        .list {
            overflow: auto;
        }
        a {
            text-decoration: none;
            font-size: 14px;
            color: #555555;
        }
        a:hover {
            color: #0095e4;
        }

        a:visited {
            color: #800080;
        }

        a:visited:hover {
            color: #b900b9;
        }

        a.nowPlaying {
            color: #000000 !important;
            background: #ff9600;
            border: 1px dotted #d67e00;
            border-radius: 5px;
        }

        .overlay {
            position: fixed;
            top: 0;
            bottom: 0;
            left: 0;
            right: 0;
            background: rgba(0, 0, 0, 0.7);
            transition: opacity 500ms;
            z-index: 42;
            display: none;
        }

        .popup,
        #popup1.day .popup,
        #popup1.night .popup {
            margin: 3em auto;
            padding: 20px;
            border-radius: 5px;
            width: 90%;
            height: 90%;
            position: relative;
            transition: all 1s ease-in-out;
        }

        #popup1.day .popup {
            background: #fff;
        }

        #popup1.night .popup {
            background: #000;
        }

        .popup h2 {
            margin-top: 0;
            color: #a19f9f;
            font-family: sans-serif;
            font-size: small;
            text-align: center;
        }

        .popup .max {
            position: absolute;
            top: 10px;
            right: 40px;
            transition: all 200ms;
            font-weight: bold;
            text-decoration: none;
            z-index: 1000;
        }
        .popup .max:hover, .popup .close:hover {
            color: #06D85F;
        }

        .popup .close {
            position: absolute;
            top: 10px;
            right: 10px;
            transition: all 200ms;
            font-weight: bold;
            text-decoration: none;
            z-index: 1000;
        }

        #popup1 .content>img {
            max-width: 100%;
            max-height: 90%;
            margin: auto;
            display: block;
            margin: auto;
            top:0;
            bottom:0;
            left:0;
            right:0;
            position:absolute;
        }

        #popup1.day .popup .close, 
        #popup1.day .popup .max {
            color: #333;
        }

        #popup1.night .popup .close, 
        #popup1.night .popup .max {
            color: #e1e0e0;
        }

        .popup .content {
            height: 100%;
            overflow: auto;
        }

        #popup1.night .popup {
            background-color: black;
        }

        #popup1.day .popup {
            background-color: white;
        }
        .popup video {
            max-width: 100%;
            max-height: 100%;
            width: 100%;
            margin: auto;
            position: absolute;
            left: 0;
            top: 0;
            right: 0;
            bottom: 0;
        }

        .popup video::cue {
            color: #ffffff;
            border: 3px solid black;
            text-align: center;
            text-shadow: -1px -1px 1px rgba(255,255,255,.1), 1px 1px 1px rgba(0,0,0,.5), 2px 2px 3px rgba(206,89,55,0);
            font-size: 1.5vw;
        }

        .fixedplayer {
            display: none;
            text-align: center;
            background: #f1f3f4;
            position: fixed;
            min-width: 67vw;
            width: 100%;
            height: 24px;
            padding: 0;
            top: 0;
            right: 0;
            left:0;
            z-index: 3;
        }

        .fixedplayer #aplayer{
            width: 432px;
            height: 24px;
        }

        .fixedplayer a {
            position: absolute;
            top: 2px;
            transition: all 200ms;
            font-weight: bold;
            text-decoration: none;
            z-index: 1000;
            color: #939393;
        }
        .fixedplayer a:hover {
            color: #f5c400;
        }

        .maximized {
                width: 98% !important;
                height: 100% !important;
                margin: auto !important;
        }
        .meta {
            font-size:10px;
            color: #ccc;
        }
        .nowPlaying .meta {
            font-size:10px;
            color: #775f89;            
        }
        @media (max-width: 811px) {
            #maine {
                min-height: 100vh;
                grid-template-columns: repeat(auto-fill, minmax(15em, 1fr)); 
                gap: 2px;
            }
            item.img, item.video {
                max-width: 50vw;
                height: auto;
                display: table-cell;
            }        
        }
        </style>
        <script>
            document.onkeydown = function (e) {
            const v = document.querySelector('.popup video');
            const img = document.querySelector('.popup img');

            switch (e.which) {
                case 27:    // = Escape
                    break;
                case 37: // left
                    if (v){
                        if (!v.paused) {
                            v.pause();
                            v.currentTime = v.currentTime - 5.0;
                            if (v.paused) {
                                v.play();
                            }
                        }
                    } else if (window.currentLink && (img || !v || (v && v.paused && v.currentTime == 0))) {
                        const prev = findNextPrevLink(window.currentLink, REGEX_TYPE_CAN_PREVIEW, true);
                        if (prev) {
                            onLinkClicked(prev);
                        }
                    }
                    break;
                case 32: // Space
                    if(e.which == 32){
                    // stops default behaviour of space bar. Stop page scrolling down
                        e.preventDefault();
                        if (v.paused) {
                            v.play();
                        } else {
                            v.pause();
                        }
                    }
                    break;
                case 39: // Right
                    if (v && e.which != 32){
                        if (!v.paused) {
                            v.pause();
                            v.currentTime = v.currentTime + 5.0;
                            if (v.paused) {
                                v.play();
                            }
                        }
                    }
                    if (window.currentLink && (img || !v || (v && v.ended)) ) {
                        const next = findNextPrevLink(window.currentLink, REGEX_TYPE_CAN_PREVIEW);
                        if (next) {
                            onLinkClicked(next);
                        }
                    }

                    break;
                case 70: // f-key
                    if (!(e.shiftKey || e.ctrlKey || e.altKey || e.metaKey)) {
                        if (!window.isFs) {
                            window.isFs = true;
                            fullscreenOn(v || img || document);
                        } else {
                            window.isFs = false;
                            fullscreenOff(v || img || document);
                        }
                    }
                    break;
            }
        };

        function fullscreenOn(p) {
            var fs = p.requestFullscreen || p.webkitRequestFullscreen || p.mozRequestFullScreen || p.oRequestFullscreen || p.msRequestFullscreen;
            fs.call(p);
        }

        function fullscreenOff(p) {
            var fsx = p.exitFullScreen || p.webkitExitFullScreen || p.mozExitFullScreen || p.oExitFullScreen || p.msExitFullScreen;
            fsx.call(p);
        }

        function findClosest(el, tagName, className) {
            if (el.tagName === tagName && !className || el.classList.contains(className)) {
                return el;
            } else if (el.parentElement) {
                return findClosest(el.parentElement, tagName, className);
            }
            return null;
        }

        function onDocumentClickHandler(e) {
            const el = e.target;

            let link = el;

            if (link.tagName !== 'A') {
                link = findClosest(link, 'A');
            }

            if (link && (link.matches("a"))) {
                onLinkClicked(link, e);
            }
        }

          function findNextPrevLink(link, regex, isBackwards) {
            // find next/prev link, if no more songs in current album <p..>
            // then move on to the next album <div class="item"..>
            for (var [tagName, className] of [['P', null], ['DIV', 'item']]) {
              console.log(`Tag: ${tagName}, Class: ${className}`);

              var item = findClosest(link, tagName, className);

              while (item && (item = isBackwards ? item.previousElementSibling : item.nextElementSibling)) {

                var links = item.querySelectorAll('a');
                for (let i = 0; i < links.length; i++) {
                  const a = links[i];
                  if (regex.test(a.getAttribute('href'))) {
                    return a;
                  } 
                }
              }
            }
        }

const REGEX_TYPE_AUDIO = /\.(mp3|m4a|aac|flac|ape|wav|ogg|oga|webm)$/i;
const REGEX_TYPE_VIDEO = /\.(mp4|m4v|avi|mov|mpg|mpeg|ogv|ogm|opus|mkv)$/i;
const REGEX_TYPE_IMAGE = /\.(gif|jpe?g|a?png|tiff?|bmp|webp|ico|wmf|avif|svg)$/i;
const REGEX_TYPE_CODE = /\.(asp|txt|memo|kt|go|ics|rst?|rb|dart|php|js|tsx?|py|cue|ipynb|z?sh|xml|plist|bat|css|json|java|c|cpp|h|m|hpp|conf|ini|pl|yaml|yml|groovy|swift|properties|gradle|srt|sql|lua|m3u8|log?)$/i;
const REGEX_TYPE_MARKDOWN = /\.(md)$/i;
const REGEX_TYPE_CAN_PREVIEW = /\.(mp4|m4v|avi|mov|mpg|mpeg|ogv|ogm|opus|mkv|md|html?|gif|jpe?g|a?png|tiff?|bmp|webp|ico|wmf|avif|svg|asp|txt|memo|kt|go|ics|rst?|rb|dart|php|js|tsx?|py|cue|ipynb|z?sh|xml|plist|bat|css|json|java|c|cpp|h|m|hpp|conf|ini|pl|yaml|yml|groovy|swift|properties|gradle|srt|sql|lua|m3u8?)$/i;

function onAudioEnded(el) {
    if (el && window.currentLink) {
        window.currentLink.classList.remove('nowPlaying');
        let nextLink = findNextPrevLink(window.currentLink, REGEX_TYPE_AUDIO);
        if (!nextLink) {
            return;
        }
        const href = nextLink.getAttribute('href');

        if (REGEX_TYPE_AUDIO.test(href)) {
            el.src = href;
            el.play();
            el.title = decodeURIComponent(href);
            nextLink.classList.add('nowPlaying');
            nextLink.scrollIntoView(
                {
                    behavior: "smooth",
                    block: "center"
                }
            );
            window.currentLink = nextLink;
        }
    }
}

function startAudioPlayer(href) {
    const audioPlayer = document.getElementById('aplayer');
        audioPlayer.parentElement.style.display = 'block';
        audioPlayer.src = href;
        audioPlayer.title = decodeURIComponent(href);
        audioPlayer.play();
        //document.querySelectorAll('th').forEach((th) => th.classList.toggle('offset'));
}

function hideAudioPlayer() {
    const audioPlayer = document.getElementById('aplayer');
    audioPlayer.pause();
    audioPlayer.parentElement.style.display = 'none';
    //document.querySelectorAll('th').forEach((th) => th.classList.toggle('offset'));

    if (window.currentLink) {
        window.currentLink.classList.remove('nowPlaying');
    }
}

function onLinkClicked(link, evt) {
    var href = link.getAttribute("href");

    if (href == '#') {
        return;
    }

    if (window.currentLink && REGEX_TYPE_AUDIO.test(href)) {
        document.querySelectorAll('.nowPlaying').forEach(element => {
          element.classList.remove('nowPlaying');
        });
    }
    window.currentLink = link;

    const event = evt || new Event('click');

    const fileName = href.split('/').pop();
    const baseName = fileName.split('.')[0];

    if (REGEX_TYPE_VIDEO.test(href)) {
        event.preventDefault();
        event.stopImmediatePropagation();

        hideAudioPlayer();

        if (link.firstElementChild && !link.firstElementChild.paused) {
            link.firstElementChild.pause();
        }

        const video = document.createElement('video');
        video.setAttribute('autoplay', true);
        video.setAttribute('controls', 'controls');
        video.setAttribute('preload', 'auto');
        video.setAttribute('tabIndex', "-1");

        const source = document.createElement('source');

        source.src = href;
        video.appendChild(source);

        document.querySelectorAll('a').forEach((link) => {
            if (new RegExp(`${baseName}\.?.*\.(vtt|srt)`, 'i').test(link.href)) {
                const track = document.createElement('track');
                loadSubtitle(link.href)
                    .then(url => {
                        track.src = url;
                        track.kind = 'subtitles';
                        track.label = decodeURIComponent(link.href.split('/').pop());
                        track.srclang = 'en';

                        if (!video.hasDefaultSubs) {
                            track.default = true;
                            video.hasDefaultSubs = true;
                        }

                        video.appendChild(track);
                    })
            }
        });

        content.innerHTML = '';
        content.appendChild(video);

    }else if (REGEX_TYPE_AUDIO.test(href)) {
        event.preventDefault();
        event.stopImmediatePropagation();

        startAudioPlayer(href);
        link.classList.add('nowPlaying');

    } else if (window.location.search && !link.href.includes(window.location.search)) { // regular click
        event.preventDefault();
        event.stopImmediatePropagation();
        document.location.href = `${link.href}${window.location.search}`;
    }

}
document.onclick = onDocumentClickHandler;                
        </script>
    </head>
    <body>
        <div class="fixedplayer">
            <audio controls id="aplayer" src="" onended="onAudioEnded(this)"></audio><a href="#" onclick="hideAudioPlayer(); return false;">&times;</a>
        </div>'''
                   f'''<h1><a href="..">..</a> {args.gallery_root.absolute()}</h1>
        <div id="maine">'''
                   + html_data
                   + '''</div>
    </body>
</html>
''')

    print(f"Gallery HTML file generated: {output_path.absolute()}")


if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser(description='Music gallery Generator')
    parser.add_argument('gallery_root',
                        help='Gallery root, by default current folder',
                        nargs='?',
                        action='store',
                        type=pathlib.Path,
                        default=".")
    parser.add_argument('--output-file', '-o',
                        metavar='output_file',
                        default=OUTPUT_FILE_NAME,
                        help='Output filename')
    parser.add_argument('--videos', '-m',
                        default=False,
                        action='store_true',
                        help='Include videos')
    parser.add_argument('--ignored', '-i',
                        nargs='*',
                        metavar='ignore',
                        default=[],
                        help='''Custom ignored path segments.
                        Accepts multiple segments, e.g. -i junk1 junk2 junk3 "%s"''' % [])
    parser.add_argument('--verbose', '-v',
                        help='Verbose output',
                        default=False,
                        action='store_true')

    args = parser.parse_args(sys.argv[1:])

    print(args)
    print(f'Collecting media in: {args.gallery_root.absolute()}...')
    chunks: list[str] = collect_media_assets(args)

    if not chunks:
        print("No media files found.")
    else:

        html_content: str = '\n'.join(chnk for chnk in chunks)

        generate_gallery_html(html_content, args)

        if len(chunks):
            print(f'''
Total chunks:    {len(chunks)}
Images:          {total_images}
Audios:          {total_audios}
Videos:          {total_videos}
''')
