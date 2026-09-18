package com.pixelwa.companion

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.Settings
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.pixelwa.companion.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {
    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: Prefs

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = Prefs(this)

        binding.serverUrl.setText(prefs.serverBaseUrl)
        binding.apiToken.setText(prefs.apiToken)
        binding.masterSwitch.isChecked = prefs.automationEnabled
        binding.vpnWatchdogSwitch.isChecked = prefs.vpnWatchdogEnabled

        binding.saveBtn.setOnClickListener {
            prefs.serverBaseUrl = binding.serverUrl.text?.toString().orEmpty()
            prefs.apiToken = binding.apiToken.text?.toString().orEmpty()
            prefs.automationEnabled = binding.masterSwitch.isChecked
            prefs.vpnWatchdogEnabled = binding.vpnWatchdogSwitch.isChecked
            applyService()
            requestMediaPermissions()
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
            binding.statusText.text =
                "Status: ${if (prefs.automationEnabled) "ON" else "OFF"} @ ${prefs.serverBaseUrl}" +
                    if (prefs.vpnWatchdogEnabled) " · VPN watchdog" else ""
        }

        binding.masterSwitch.setOnCheckedChangeListener { _, checked ->
            prefs.automationEnabled = checked
            applyService()
        }
        binding.vpnWatchdogSwitch.setOnCheckedChangeListener { _, checked ->
            prefs.vpnWatchdogEnabled = checked
        }

        binding.openAccessibility.setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        binding.openNotifications.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }
        binding.openStorage.setOnClickListener {
            openAllFilesAccess()
        }

        binding.statusText.text =
            "Status: ${if (prefs.automationEnabled) "ON" else "OFF"} @ ${prefs.serverBaseUrl}" +
                if (prefs.vpnWatchdogEnabled) " · VPN watchdog" else ""
        applyService()
        requestMediaPermissions()
    }

    private fun requestMediaPermissions() {
        val needed = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 33) {
            listOf(
                Manifest.permission.READ_MEDIA_IMAGES,
                Manifest.permission.READ_MEDIA_VIDEO,
                Manifest.permission.READ_MEDIA_AUDIO,
            ).forEach { p ->
                if (ContextCompat.checkSelfPermission(this, p) != PackageManager.PERMISSION_GRANTED) {
                    needed.add(p)
                }
            }
        } else if (Build.VERSION.SDK_INT >= 23) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.READ_EXTERNAL_STORAGE)
                != PackageManager.PERMISSION_GRANTED
            ) {
                needed.add(Manifest.permission.READ_EXTERNAL_STORAGE)
            }
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), REQ_MEDIA)
        }
    }

    private fun openAllFilesAccess() {
        if (Build.VERSION.SDK_INT >= 30) {
            if (Environment.isExternalStorageManager()) {
                Toast.makeText(this, "All files access already granted", Toast.LENGTH_SHORT).show()
                return
            }
            try {
                startActivity(
                    Intent(
                        Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
                        Uri.parse("package:$packageName"),
                    )
                )
            } catch (_: Exception) {
                startActivity(Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION))
            }
        } else {
            requestMediaPermissions()
            Toast.makeText(this, "Requested storage permission", Toast.LENGTH_SHORT).show()
        }
    }

    private fun applyService() {
        val intent = Intent(this, PollingForegroundService::class.java)
        if (prefs.automationEnabled) {
            ContextCompat.startForegroundService(this, intent)
        } else {
            stopService(intent)
        }
    }

    companion object {
        private const val REQ_MEDIA = 1001
    }
}
