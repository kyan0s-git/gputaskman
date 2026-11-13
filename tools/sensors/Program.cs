// gputaskman-sensors: prints LibreHardwareMonitor sensor readings as JSON lines.
//
//   gputaskman-sensors.exe [--once] [--interval MS]
//
// Each line is one reading of every sensor:
//   {"t":<unix s>,"admin":bool,"pawnio":bool,"error":null|"...","hardware":[{"id","name","type","parent",
//     "sensors":[{"id","name","type","value":null|number,"min","max"}]}],
//    "disks":[{"device_id","model","serial","media_type","bus_type","size","health_status","operational_status",
//              "temperature","temperature_max","power_on_hours","wear","read_errors","write_errors"}],"disk_error":null|"..."}
// Drive health comes from the Windows Storage Management API (MSFT_PhysicalDisk; the reliability counters need
// administrator rights) and is refreshed every minute on its own thread, so it never delays a sensor reading.
// A value LibreHardwareMonitor cannot read is null, never 0. The helper only reads: it never sets fan speeds,
// clocks, or anything else LibreHardwareMonitor can control. It exits when stdin closes, so it never outlives
// gputaskman.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Management;
using System.Security.Principal;
using System.Text;
using System.Threading;
using LibreHardwareMonitor.Hardware;
using Microsoft.Win32;

namespace GpuTaskmanSensors
{
    internal static class Program
    {
        private static int Main(string[] args)
        {
            bool once = false;
            int interval = 1000;
            for (int i = 0; i < args.Length; i++)
            {
                if (args[i] == "--once") once = true;
                else if (args[i] == "--interval" && i + 1 < args.Length &&
                         int.TryParse(args[++i], NumberStyles.Integer, CultureInfo.InvariantCulture, out int ms))
                    interval = Math.Max(250, Math.Min(60000, ms));
                else if (args[i] == "--help" || args[i] == "-h")
                {
                    Console.WriteLine("usage: gputaskman-sensors [--once] [--interval MS]");
                    return 0;
                }
            }

            var stdout = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\n" };
            bool admin = IsAdministrator();
            bool pawnio = PawnIoInstalled();
            var computer = new Computer
            {
                IsCpuEnabled = true,
                IsGpuEnabled = true,
                IsMemoryEnabled = true,
                IsMotherboardEnabled = true,
                IsControllerEnabled = true,
                IsStorageEnabled = true,
                IsPsuEnabled = true,
                IsBatteryEnabled = true,
                IsNetworkEnabled = false, // gputaskman reads network adapters itself
            };
            try
            {
                computer.Open();
            }
            catch (Exception e)
            {
                stdout.WriteLine(ErrorLine(admin, pawnio, "LibreHardwareMonitor failed to open: " + e.GetType().Name + ": " + e.Message));
                return 1;
            }

            var stop = new ManualResetEvent(false);
            if (!once)
            {
                // The parent closes our stdin when it stops (or dies): exit then, never linger.
                var watcher = new Thread(() =>
                {
                    try { while (Console.In.Read() != -1) { } } catch { }
                    stop.Set();
                }) { IsBackground = true };
                watcher.Start();
            }

            if (!once)
                new Thread(DiskLoop) { IsBackground = true }.Start();
            else
                QueryDisks();

            var visitor = new UpdateVisitor();
            try
            {
                computer.Accept(visitor); // the first update primes rate-based sensors (power from energy counters)
                if (once) Thread.Sleep(1000);
                while (true)
                {
                    computer.Accept(visitor);
                    try { stdout.WriteLine(Reading(computer, admin, pawnio)); }
                    catch (IOException) { break; } // parent went away
                    if (once || stop.WaitOne(interval)) break;
                }
            }
            finally
            {
                try { computer.Close(); } catch { }
            }
            return 0;
        }

        private static readonly object DiskLock = new object();
        private static string _disks = "[]";
        private static string _diskError;

        private static void DiskLoop()
        {
            while (true)
            {
                QueryDisks();
                Thread.Sleep(60000);
            }
        }

        private static void QueryDisks()
        {
            var sb = new StringBuilder("[");
            string error = null;
            try
            {
                var scope = new ManagementScope(@"\\.\root\Microsoft\Windows\Storage");
                scope.Connect();
                var query = new ObjectQuery("SELECT * FROM MSFT_PhysicalDisk");
                bool first = true;
                using (var searcher = new ManagementObjectSearcher(scope, query))
                {
                    foreach (ManagementObject disk in searcher.Get())
                    {
                        if (!first) sb.Append(',');
                        first = false;
                        sb.Append("{\"device_id\":"); Json.Str(sb, Prop(disk, "DeviceId"));
                        sb.Append(",\"model\":"); Json.Str(sb, Prop(disk, "FriendlyName"));
                        sb.Append(",\"serial\":"); Json.Str(sb, Prop(disk, "SerialNumber"));
                        sb.Append(",\"media_type\":"); Json.Raw(sb, Prop(disk, "MediaType"));
                        sb.Append(",\"bus_type\":"); Json.Raw(sb, Prop(disk, "BusType"));
                        sb.Append(",\"size\":"); Json.Raw(sb, Prop(disk, "Size"));
                        sb.Append(",\"health_status\":"); Json.Raw(sb, Prop(disk, "HealthStatus"));
                        sb.Append(",\"operational_status\":"); Json.Str(sb, OperationalStatus(disk));
                        try
                        {
                            // Administrator only: without it this throws "Access denied" and the fields stay null.
                            foreach (ManagementObject rc in disk.GetRelated("MSFT_StorageReliabilityCounter"))
                            {
                                sb.Append(",\"temperature\":"); Json.Raw(sb, Prop(rc, "Temperature"));
                                sb.Append(",\"temperature_max\":"); Json.Raw(sb, Prop(rc, "TemperatureMax"));
                                sb.Append(",\"power_on_hours\":"); Json.Raw(sb, Prop(rc, "PowerOnHours"));
                                sb.Append(",\"wear\":"); Json.Raw(sb, Prop(rc, "Wear"));
                                sb.Append(",\"read_errors\":"); Json.Raw(sb, Prop(rc, "ReadErrorsTotal"));
                                sb.Append(",\"write_errors\":"); Json.Raw(sb, Prop(rc, "WriteErrorsTotal"));
                                break;
                            }
                        }
                        catch (Exception) { }
                        sb.Append('}');
                    }
                }
            }
            catch (Exception e)
            {
                error = e.GetType().Name + ": " + e.Message;
            }
            sb.Append(']');
            lock (DiskLock)
            {
                _disks = error == null ? sb.ToString() : "[]";
                _diskError = error;
            }
        }

        private static string Prop(ManagementBaseObject o, string name)
        {
            try { return o[name]?.ToString(); } catch { return null; }
        }

        private static string OperationalStatus(ManagementBaseObject o)
        {
            // Codes from the Storage Management API: 2 OK, 3 Degraded, 5 Predictive Failure, ...
            try
            {
                if (!(o["OperationalStatus"] is ushort[] codes)) return null;
                var names = new List<string>();
                foreach (var c in codes)
                {
                    switch (c)
                    {
                        case 2: names.Add("OK"); break;
                        case 3: names.Add("Degraded"); break;
                        case 5: names.Add("Predictive Failure"); break;
                        case 6: names.Add("Error"); break;
                        case 7: names.Add("Non-Recoverable Error"); break;
                        case 10: names.Add("Stopped"); break;
                        case 13: names.Add("Lost Communication"); break;
                        case 0xD00D: names.Add("Not Usable"); break;
                        default: names.Add(c.ToString(CultureInfo.InvariantCulture)); break;
                    }
                }
                return string.Join(", ", names);
            }
            catch { return null; }
        }

        private static bool IsAdministrator()
        {
            try
            {
                using (var identity = WindowsIdentity.GetCurrent())
                    return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
            }
            catch { return false; }
        }

        private static bool PawnIoInstalled()
        {
            try
            {
                using (var key = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64)
                           .OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO"))
                    return key != null;
            }
            catch { return false; }
        }

        private static string ErrorLine(bool admin, bool pawnio, string error)
        {
            var sb = new StringBuilder();
            sb.Append("{\"t\":").Append(Now()).Append(",\"admin\":").Append(admin ? "true" : "false")
              .Append(",\"pawnio\":").Append(pawnio ? "true" : "false").Append(",\"error\":");
            Json.Str(sb, error);
            sb.Append(",\"hardware\":[]}");
            return sb.ToString();
        }

        private static string Reading(Computer computer, bool admin, bool pawnio)
        {
            var sb = new StringBuilder(16384);
            sb.Append("{\"t\":").Append(Now()).Append(",\"admin\":").Append(admin ? "true" : "false")
              .Append(",\"pawnio\":").Append(pawnio ? "true" : "false").Append(",\"error\":null,\"hardware\":[");
            bool first = true;
            foreach (IHardware hw in computer.Hardware)
                WriteHardware(sb, hw, null, ref first);
            sb.Append("],\"disks\":");
            lock (DiskLock)
            {
                sb.Append(_disks).Append(",\"disk_error\":");
                if (_diskError == null) sb.Append("null"); else Json.Str(sb, _diskError);
            }
            sb.Append('}');
            return sb.ToString();
        }

        private static void WriteHardware(StringBuilder sb, IHardware hw, IHardware parent, ref bool first)
        {
            if (!first) sb.Append(',');
            first = false;
            sb.Append("{\"id\":"); Json.Str(sb, hw.Identifier.ToString());
            sb.Append(",\"name\":"); Json.Str(sb, hw.Name);
            sb.Append(",\"type\":"); Json.Str(sb, hw.HardwareType.ToString());
            sb.Append(",\"parent\":");
            if (parent == null) sb.Append("null"); else Json.Str(sb, parent.Identifier.ToString());
            sb.Append(",\"sensors\":[");
            bool firstSensor = true;
            foreach (ISensor s in hw.Sensors)
            {
                if (!firstSensor) sb.Append(',');
                firstSensor = false;
                sb.Append("{\"id\":"); Json.Str(sb, s.Identifier.ToString());
                sb.Append(",\"name\":"); Json.Str(sb, s.Name);
                sb.Append(",\"type\":"); Json.Str(sb, s.SensorType.ToString());
                sb.Append(",\"value\":"); Json.Num(sb, s.Value);
                sb.Append(",\"min\":"); Json.Num(sb, s.Min);
                sb.Append(",\"max\":"); Json.Num(sb, s.Max);
                sb.Append('}');
            }
            sb.Append("]}");
            foreach (IHardware sub in hw.SubHardware)
                WriteHardware(sb, sub, hw, ref first);
        }

        private static string Now() =>
            ((DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds).ToString("0.000", CultureInfo.InvariantCulture);
    }

    internal sealed class UpdateVisitor : IVisitor
    {
        public void VisitComputer(IComputer computer) => computer.Traverse(this);

        public void VisitHardware(IHardware hardware)
        {
            try { hardware.Update(); } catch { } // one failing device must not stop the others
            foreach (IHardware sub in hardware.SubHardware) sub.Accept(this);
        }

        public void VisitSensor(ISensor sensor) { }
        public void VisitParameter(IParameter parameter) { }
    }

    internal static class Json
    {
        public static void Num(StringBuilder sb, float? v)
        {
            if (v.HasValue && !float.IsNaN(v.Value) && !float.IsInfinity(v.Value))
                sb.Append(v.Value.ToString("R", CultureInfo.InvariantCulture));
            else
                sb.Append("null"); // unknown is not zero
        }

        /// A number as-is when it parses as one, else null (WMI hands numbers back as strings here).
        public static void Raw(StringBuilder sb, string s)
        {
            if (s != null && double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out double v) &&
                !double.IsNaN(v) && !double.IsInfinity(v))
                sb.Append(v.ToString("R", CultureInfo.InvariantCulture));
            else
                sb.Append("null");
        }

        public static void Str(StringBuilder sb, string s)
        {
            if (s == null) { sb.Append("null"); return; }
            sb.Append('"');
            foreach (char c in s ?? "")
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
        }
    }
}
