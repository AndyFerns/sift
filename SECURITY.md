# Security policy

Sift's job is to delete directories, so a bug that makes it delete the wrong thing, or lets
something else trick it into doing so, is a security problem and is treated as one.

## Reporting a vulnerability

Please report privately using GitHub's private vulnerability reporting on the
[AndyFerns/sift](https://github.com/AndyFerns/sift) repository
(**Security** tab, then **Report a vulnerability**). Do not open a public issue.

Include what you can of:

- the Sift version (`sift --version`), operating system and filesystem
- the directory layout or request that triggers the problem
- what Sift did and what you expected

Sift has not had a stable release yet. Until it does, only the latest commit on `main`
is supported.

## What counts

- Deleting, or offering to delete, anything outside the scan root.
- Following a symlink, junction or other reparse point during scanning or deletion.
- Deleting an artifact that was not explicitly selected, previewed and confirmed.
- Any way to make the local dashboard API read or delete outside its configured scan roots,
  or to drive it from another website.
- Executing code or commands from a scanned project.

## Safety model

**Scanning is read-only.** Sift lists directories and reads file metadata. It reads at most
a few bytes of specific well-known files to confirm what they are. It never executes
anything it finds.

**Helper processes.** To measure very large trees Sift may start a few copies of its own
Python interpreter running one small standard-library-only script. They list directories and
read file sizes, nothing else, and exit when the scan ends.

**Nothing is deleted automatically.** Deletion needs an explicit selection, a preview
listing exactly what will be removed, and a confirmation. There is no flag to skip these.
The core refuses to run a cleanup without the confirmation token issued for that exact
preview.

**Paths are validated twice** - when the preview is built and again immediately before
each deletion. A target is refused when it:

- is not strictly inside the scan root, or contains `..`
- is the scan root, a filesystem root, the home directory, or an ancestor of the home directory
- is a symlink, junction or reparse point, or is reached through one
- directly contains a `.git` entry
- is classified as ambiguous, or below the configured confidence threshold
- no longer matches the detector rule it had in the preview
- is not the same directory (device and inode) that was previewed

**The dashboard is local only.** It binds to loopback addresses, has no remote mode,
rejects requests whose `Host` header is not a loopback name (DNS rebinding), rejects
cross-site `Origin`s on state-changing requests, and only accepts paths under the
configured scan roots. Scan roots cannot be changed through the API.

## Known limits

- **Deletion is permanent.** Sift does not use a recycle bin and cannot restore anything.
- **Race window on Windows.** Between the final validation and the delete, another process
  with write access to the same directory could swap it. On Linux and macOS the standard
  library's file-descriptor based removal closes this for the walk itself; Windows has no
  equivalent, so a narrow window remains. The identity check makes it hard to hit, not
  impossible.
- **Hard links.** On Windows, files with several hard links (for example pnpm stores) are
  counted at full size, so the space actually freed can be lower than reported.
- **Untested paths.** The symlink and POSIX-permission tests have not been run yet: they are
  skipped on Windows, where the suite has been run, and the CI workflow has not executed.
  Junctions and simulated permission errors are tested on Windows.
- **Unusual file names.** A path that is not valid Unicode is reported as skipped and can
  never become a cleanup target.
- **Same-user threats are out of scope.** Software already running as you can delete your
  files without Sift's help.
