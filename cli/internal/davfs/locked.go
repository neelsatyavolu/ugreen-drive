package davfs

import (
	"bytes"
	"io/fs"
)

// LockedNote is the only item inside a locked encrypted personal folder, so
// the folder stays visible in Finder and says how to open it.
const LockedNote = "Locked - unlock it in the Ugreen Drive menu.txt"

var lockedNoteText = []byte(`This personal folder is encrypted and locked.

To open it, click the Ugreen Drive icon in the menu bar, then click this
folder under Shares and choose Unlock. Enter your UGOS encryption password
or key file. It stays unlocked for 24 hours (in Finder and on the website),
then locks again.
`)

func (f *FS) lockedNoteInfo() fileInfo {
	return fileInfo{name: LockedNote, size: int64(len(lockedNoteText)), mtime: f.start}
}

// noteFile is the read-only note.
type noteFile struct {
	*bytes.Reader
	info fileInfo
}

func (n *noteFile) Close() error                       { return nil }
func (n *noteFile) Stat() (fs.FileInfo, error)         { return n.info, nil }
func (n *noteFile) Readdir(int) ([]fs.FileInfo, error) { return nil, errNotSupported }
func (n *noteFile) Write([]byte) (int, error)          { return 0, errNotSupported }
