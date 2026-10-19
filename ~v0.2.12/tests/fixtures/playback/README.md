# Protocol fixtures

The six XML files retain the namespaces, tag hierarchy, spelling/case and
attribute-versus-element layout of saved bench responses from September 18–19,
2026. They were derived by reading existing files, without contacting a camera.

- `isapi-device.xml`: saved ANPVIZ deviceInfo response.
- `isapi-tracks.xml`: saved TrackList, IDs 101 and 103, both `Enable=true`.
- `isapi-search-page-0.xml` / `-1.xml`: saved MORE/OK search pages. Results were
  reduced from 50/12 to 2/1 with matching `numOfMatches`; synthetic times and
  replay locators preserve the original item structure.
- `cgi-uid.xml`: observed `uid` root, with a synthetic session value.
- `cgi-record-query.xml`: observed `RecordQueryInfo` root with repeated `items`
  elements and attributes, reduced to two synthetic recordings.

Names, IDs identifying a device, serials, credentials, sessions, URLs, filesystem
paths and dates were replaced. No private device/recording identity is retained.
Track IDs, booleans, schedule structure and public schema information are kept.

The tests deliberately construct a track-103 rejection and a mismatched returned
track. These are regression scenarios, **not captured failures of the user's
camera**. The existing evidence establishes success on 101, not failure on 103.
HTML-200 fixtures are synthetic reproductions of the saved `f404.jpg` marker;
they are still rejected before XML capability detection.
