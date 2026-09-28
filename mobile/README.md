# PayTracker Mobile App

This directory contains the Flet-based Android application. It connects to the local desktop server to sync timesheets and offline data.

## Release Workflow

Because this app supports both **Obtainium** (automated updates) and **Manual Sideloading** (in-app update prompts), you must update both the code and the GitHub tags when releasing a new version.

When you are ready to publish an update, follow these 4 steps:

### 1. Update the Internal Version
Open `mobile_app.py` and increment the `APP_VERSION` variable.
```python
APP_VERSION = "1.3"
```

### 2. Update the Public Version Tracker
Open `version.json` and increment the `latest_version`. (This file is what sideloaded apps check to know an update exists).
```json
{
  "latest_version": "1.3",
  "apk_url": "https://github.com/shaslip/faa-paytracker/releases/latest/download/app-release.apk"
}
```

### 3. Commit Your Changes
Save your files and push them to the repository:
```bash
git add .
git commit -m "Update mobile app to v1.3"
git push
```

### 4. Trigger the Build (Git Tag)
Create and push a version tag. This tells GitHub Actions to spin up a server, build the new `.apk`, and attach it to a new GitHub Release.
```bash
git tag v1.3
git push origin v1.3
```

**Done!** 
* **Obtainium** will see the new GitHub Release and update your phone automatically.
* **Normal users** will see a popup in their app telling them to download the new version.
