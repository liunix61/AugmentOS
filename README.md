# WearLLM

Wearable contextual AI thinking upgrades.

By TeamBandwidth.

### Building Android Client

0. Run Linux (as you should be).
1. Install Java 17.
2. Ensure Java 17 is default Java (can set with `sudo update-java-alternatives`).
3. Run `chmod 777 ./gradle/` and `chmod 777 ./gradle/`.
4. Set your ANDROID_SDK_PATH WITH `export $ANDROID_SDK_PATH=<path to you Android>`.
5. Go into android folder and run `bash build_all.sh` to build everything.
6. If you get gradle version issues, install gradle 8.0.2: https://linuxhint.com/installing_gradle_ubuntu/ (follow the instructions, but replace 7.4.2 with 8.0.2).
7. Subsequent builds, you can just run `assembleDebug --stacktrace` to build the APK.
8. Install APK on phone (located in app/build/outputs/debug/).
