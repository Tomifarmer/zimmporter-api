"""Command-line interface for zimmporter.

Run via ``python -m zimmporter`` to search YouTube Music or trigger
downloads directly (without the FastAPI layer).
"""

import argparse

import emoji

from zimmporter.core import Zimmporter

parser = argparse.ArgumentParser(description="Download awesome songs'")
sp = parser.add_subparsers(help="Action", dest="action", required=True)
search_parser = sp.add_parser("search", help="Search songs, artist, albums on YT Music")
search_parser.add_argument("query")
search_parser.add_argument("--playlist", action="store_true")
download_parser = sp.add_parser("download", help="Download songs from YT Musics")
download_parser.add_argument("id")
download_parser.add_argument("-p", "--playlist", action="store_true")
download_parser.add_argument("-c", "--concurrent", default=4, type=int)

args = parser.parse_args()


def main() -> None:
    """Parse CLI arguments and dispatch to search or download actions."""
    zimmporter = Zimmporter()

    if args.action == "search":
        results = zimmporter.search(args.query, filter="community_playlists" if args.playlist else "albums")
        for r in results:
            if r["resultType"] in ["song", "video"]:
                print(emoji.emojize(":round_pushpin: " + r["videoId"]))
                print(emoji.emojize(f"\t:eye: {r['resultType']}"))
                print(emoji.emojize(f"\t:musical_note: {r['title']}"))
                if len(r["artist"]) > 0:
                    print(emoji.emojize(f"\t:man_singer: {r['artist'][0]}"))
                print(emoji.emojize(f"\t:watch: {r['duration']}"))
            elif r["resultType"] == "album":
                print(emoji.emojize(":round_pushpin: " + r["browseId"]))
                print(emoji.emojize(f"\t:optical_disk: {r['title']}"))
                print(emoji.emojize(f"\t:floppy_disk: {r['type']}"))
                print(emoji.emojize(f"\t:man_singer: {r['artist'][0]}"))
                print(emoji.emojize(f"\t:calendar: {r['year']}"))
            elif r["resultType"] == "artist":
                print(emoji.emojize(f":singer: {r['name'][0]}"))
                print(emoji.emojize(f":family: {r['subscribers']}"))
            elif r["resultType"] in ["featured_playlists", "playlist"]:
                print(emoji.emojize(":round_pushpin: " + r["browseId"]))
                print(emoji.emojize(f"\t:optical_disk: {r['title']}"))
                print(emoji.emojize(f"\t:floppy_disk: {r['resultType']}"))
                print(emoji.emojize(f"\t:man_singer: {r['author']}"))
            print("-" * 30)

    elif args.action == "download":
        if args.playlist:
            zimmporter.download_bulk(args.id, album=False, playlist=True, concurrent=args.concurrent)
        else:
            zimmporter.download_bulk(args.id, concurrent=args.concurrent)


if __name__ == "__main__":
    main()
