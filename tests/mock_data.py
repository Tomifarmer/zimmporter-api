MOCK_SEARCH_RESPONSE = [
    {
        "resultType": "album",
        "browseId": "MPREb_abc123",
        "title": "Test Album",
        "year": "2024",
        "type": "Album",
        "artists": [{"name": "Test Artist"}],
        "thumbnails": [
            {"url": "https://example.com/thumb_small.jpg", "width": 60, "height": 60},
            {"url": "https://example.com/thumb_large.jpg", "width": 300, "height": 300},
        ],
    },
    {
        "resultType": "album",
        "browseId": "MPREb_def456",
        "title": "Another Album",
        "year": "2023",
        "type": "EP",
        "artists": [{"name": "Another Artist"}],
        "thumbnails": [
            {"url": "https://example.com/thumb2.jpg", "width": 120, "height": 120},
        ],
    },
    {
        "resultType": "song",
        "videoId": "vid_song_001",
        "title": "Test Song",
        "artists": [{"name": "Song Artist"}],
        "duration": "3:45",
        "thumbnails": [{"url": "https://example.com/song_thumb.jpg", "width": 120, "height": 120}],
    },
    {
        "resultType": "artist",
        "name": "Test Artist",
        "artists": [{"name": "Test Artist"}],
        "subscribers": "1.2M",
        "thumbnails": [{"url": "https://example.com/artist.jpg", "width": 200, "height": 200}],
    },
    {
        "resultType": "playlist",
        "browseId": "VLplay_001",
        "title": "Test Playlist",
        "author": "Test Author",
        "itemCount": 15,
        "thumbnails": [{"url": "https://example.com/pl_thumb.jpg", "width": 120, "height": 120}],
    },
    {
        "resultType": "featured_playlists",
        "browseId": "VLfeat_001",
        "title": "Featured Playlist",
        "author": "YT Music",
        "itemCount": 25,
        "thumbnails": [{"url": "https://example.com/feat_thumb.jpg", "width": 120, "height": 120}],
    },
]

MOCK_ALBUM_DATA = {
    "title": "Test Album",
    "year": 2024,
    "artists": [{"name": "Test Artist"}],
    "thumbnails": [
        {"url": "https://example.com/album_cover.jpg", "width": 300, "height": 300},
    ],
    "tracks": [
        {
            "title": "Track One",
            "videoId": "vid_track1",
            "trackNumber": 1,
            "artists": [{"name": "Test Artist"}],
            "thumbnails": [{"url": "https://example.com/track1.jpg", "width": 120, "height": 120}],
        },
        {
            "title": "Track Two",
            "videoId": "vid_track2",
            "trackNumber": 2,
            "artists": [{"name": "Test Artist"}],
            "thumbnails": [{"url": "https://example.com/track2.jpg", "width": 120, "height": 120}],
        },
        {
            "title": "Track Three",
            "videoId": "vid_track3",
            "trackNumber": 3,
            "artists": [{"name": "Test Artist"}],
            "thumbnails": [{"url": "https://example.com/track3.jpg", "width": 120, "height": 120}],
        },
    ],
}

MOCK_ALBUM_DATA_WITH_SLASHES = {
    "title": "Album / Name",
    "year": 2024,
    "artists": [{"name": "Artist / Name"}],
    "thumbnails": [{"url": "https://example.com/cover.jpg", "width": 300, "height": 300}],
    "tracks": [
        {
            "title": "Song / Title",
            "videoId": "vid_slash",
            "trackNumber": 1,
            "artists": [{"name": "Artist / Name"}],
            "thumbnails": [{"url": "https://example.com/slash.jpg", "width": 120, "height": 120}],
        },
    ],
}

MOCK_ALBUM_DATA_NO_YEAR = {
    "title": "No Year Album",
    "artists": [{"name": "No Year Artist"}],
    "thumbnails": [{"url": "https://example.com/noyear.jpg", "width": 300, "height": 300}],
    "tracks": [
        {
            "title": "Timeless Song",
            "videoId": "vid_noyear",
            "trackNumber": 1,
            "artists": [{"name": "No Year Artist"}],
            "thumbnails": [{"url": "https://example.com/noyear_track.jpg", "width": 120, "height": 120}],
        },
    ],
}

MOCK_PLAYLIST_DATA = {
    "title": "Test Playlist",
    "thumbnails": [
        {"url": "https://example.com/playlist_cover.jpg", "width": 300, "height": 300},
    ],
    "tracks": [
        {
            "title": "Playlist Track 1",
            "videoId": "vid_pl1",
            "artists": [{"name": "Artist One"}],
            "thumbnails": [{"url": "https://example.com/pl_track1.jpg", "width": 120, "height": 120}],
        },
        {
            "title": "Playlist Track 2",
            "videoId": "vid_pl2",
            "artists": [{"name": "Artist Two"}],
            "thumbnails": [{"url": "https://example.com/pl_track2.jpg", "width": 120, "height": 120}],
        },
        {
            "title": "Unavailable Track",
            "videoId": None,
            "artists": [{"name": "Ghost Artist"}],
            "thumbnails": [{"url": "https://example.com/ghost.jpg", "width": 120, "height": 120}],
        },
    ],
}

MOCK_SONG_INFO_DICT = {
    "filepath": "/tmp/test_song.m4a",
    "ext": "m4a",
}
