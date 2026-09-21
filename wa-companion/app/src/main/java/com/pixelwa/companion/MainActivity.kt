package com.pixelwa.companion

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.PowerManager
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
            requestRuntimePermissions()
            requestBatteryExemption()
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
            refreshStatus()
        }

        binding.masterSwitch.setOnCheckedChangeListener { _, checked ->
            prefs.automationEnabled = checked
            applyService()
            refreshStatus()
        }
        binding.vpnWatchdogSwitch.setOnCheckedChangeListener { _, checked ->
            prefs.vpnWatchdogEnabled = checked
            refreshStatus()
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

        applyService()
        requestRuntimePermissions()
        requestBatteryExemption()
        ServiceWatchdog.ensureAlive(this, force = true)
        refreshStatus()
    }

    override fun onResume() {
        super.onResume()
        if (intent?.getBooleanExtra(EXTRA_WATCHDOG_RESTART, false) == true) {
            prefs.automationEnabled = true
            binding.masterSwitch.isChecked = true
            applyService()
            intent?.removeExtra(EXTRA_WATCHDOG_RESTART)
        }
        ServiceWatchdog.ensureAlive(this, force = true)
        refreshStatus()
    }

    private fun refreshStatus() {
        val poll = if (PollingForegroundService.isAliveRecently()) "poll OK" else "poll ?"
        val a11y = ServiceWatchdog.accessibilityStatusLine(this)
        binding.statusText.text =
            "Status: ${if (prefs.automationEnabled) "ON" else "OFF"} @ ${prefs.serverBaseUrl}" +
                (if (prefs.vpnWatchdogEnabled) " · VPN watchdog" else "") +
                "\n$poll · $a11y · alert SMS ${prefs.alertSmsTo}"
    }

    private fun requestRuntimePermissions() {
        val needed = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= 33) {
            listOf(
                Manifest.permission.READ_MEDIA_IMAGES,
                Manifest.permission.READ_MEDIA_VIDEO,
                Manifest.permission.READ_MEDIA_AUDIO,
                Manifest.permission.POST_NOTIFICATIONS,
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
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.SEND_SMS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.SEND_SMS)
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), REQ_RUNTIME)
        }
    }

    private fun requestBatteryExemption() {
        if (Build.VERSION.SDK_INT < 23) return
        val pm = getSystemService(PowerManager::class.java) ?: return
        if (pm.isIgnoringBatteryOptimizations(packageName)) return
        try {
            startActivity(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                    data = Uri.parse("package:$packageName")
                }
            )
        } catch (_: Exception) {
            try {
                startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            } catch (_: Exception) {
            }
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
            requestRuntimePermissions()
            Toast.makeText(this, "Requested storage permission", Toast.LENGTH_SHORT).show()
        }
    }

    private fun applyService() {
        if (prefs.automationEnabled) {
            val ok = PollingForegroundService.ensureStarted(this)
            if (!ok) {
                Toast.makeText(this, "Polling service failed to start", Toast.LENGTH_LONG).show()
            }
        } else {
            stopService(Intent(this, PollingForegroundService::class.java))
        }
    }

    companion object {
        private const val REQ_RUNTIME = 1001
        const val EXTRA_WATCHDOG_RESTART = "watchdog_restart"
    }
}
