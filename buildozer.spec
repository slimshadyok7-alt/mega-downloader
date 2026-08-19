[app]

title = MEGA Downloader

package.name = megadownloader

package.domain = org.megadownloader

source.dir = .

source.include_exts = py,png,jpg,jpeg,kv,atlas,json,txt

version = 1.0

requirements = python3,kivy,kivymd,requests,plyer

orientation = portrait

fullscreen = 0

android.permissions = INTERNET,ACCESS_NETWORK_STATE,WAKE_LOCK

android.api = 35

android.minapi = 24

android.archs = arm64-v8a

android.accept_sdk_license = True

android.allow_backup = True

android.debug_artifact = apk

android.release_artifact = aab


[buildozer]

log_level = 2

warn_on_root = 1
