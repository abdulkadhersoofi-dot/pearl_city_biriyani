# ARFA Scan release build (R8). The libraries bring their own keep rules;
# these only silence references to optional classes they never load.
# EncryptedSharedPreferences (Tink):
-dontwarn com.google.errorprone.annotations.**
-dontwarn javax.annotation.**
-dontwarn com.google.api.client.http.**
-dontwarn org.joda.time.**
# OkHttp's optional TLS providers:
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
# Line numbers in crash reports.
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
