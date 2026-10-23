# node_modules Cleaner

A desktop tool that finds `node_modules` folders and lets you choose which ones to delete. Nested folders are included in their parent's deletion and are not listed separately.

## Requirements

- Python 3.10 or newer with Tkinter.
- No pip packages are required.

Check Tkinter with `python -m tkinter`. If it is missing, install your system's Tkinter package, such as `python3-tk` on Ubuntu or Debian.

## Usage

1. Run `python byebye.py` from the folder containing the script. On systems that use `python3`, run `python3 byebye.py` instead.
2. Click **Browse** to choose a folder, or enter a path and click **Scan**.
3. Review the results. All listed folders are checked by default; uncheck anything you want to keep.
4. Click **Delete Selected** and confirm.

Choosing a `node_modules` folder directly lists that folder for deletion. Changing the root folder clears the old results.

## Important

- Deletion is permanent and bypasses the Recycle Bin or Trash.
- The scan does not follow symbolic links or Windows junctions. Matching links are removed without deleting their targets; the scan root must be a real directory.
- Windows long paths and read-only files are supported. Access errors and failed deletions are reported; a failed deletion may leave a partially removed folder.
- Close development servers and avoid moving or replacing folders while the tool runs. Wait for deletion to finish before closing the app.
- Reinstall project dependencies afterward with your project's package manager, for example `npm install`.
