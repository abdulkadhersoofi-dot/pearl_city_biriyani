# ARFA Warehouse release build (R8). The libraries bring their own keep
# rules; these only silence references to optional classes they never load.
-dontwarn com.google.errorprone.annotations.**
-dontwarn javax.annotation.**
-dontwarn com.google.api.client.http.**
-dontwarn org.joda.time.**
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
# Room's generated database class is found by name.
-keep class * extends androidx.room.RoomDatabase { <init>(); }
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile
