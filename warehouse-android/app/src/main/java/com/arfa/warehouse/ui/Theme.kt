package com.arfa.warehouse.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

val Green = Color(0xFF1F7A4D)
val GreenDark = Color(0xFF123F29)
val Good = Color(0xFF1E8E3E)
val Bad = Color(0xFFC62828)
val Warn = Color(0xFFB26A00)

private val Light = lightColorScheme(
    primary = Green, onPrimary = Color.White, primaryContainer = Color(0xFFD6EFE1), onPrimaryContainer = GreenDark,
    secondary = Color(0xFF4F6354), background = Color(0xFFF6F8F5), surface = Color.White,
    surfaceVariant = Color(0xFFE8EDE7), error = Bad,
)

private val Dark = darkColorScheme(
    primary = Color(0xFF7FD3A5), onPrimary = Color(0xFF00391F), primaryContainer = Color(0xFF1B5236),
    onPrimaryContainer = Color(0xFFD6EFE1), secondary = Color(0xFFB4CCB9), background = Color(0xFF111512),
    surface = Color(0xFF1A1F1B), surfaceVariant = Color(0xFF2A322C), error = Color(0xFFFF8A80),
)

@Composable
fun WarehouseTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = if (isSystemInDarkTheme()) Dark else Light, content = content)
}
