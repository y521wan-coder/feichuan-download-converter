namespace AccessibleVideoToText.Infrastructure;

public sealed class LocalDataPaths
{
    public LocalDataPaths(string? baseDirectory = null)
    {
        BaseDirectory = baseDirectory ?? Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "AccessibleVideoToText");
        SettingsFile = Path.Combine(BaseDirectory, "settings.json");
        CredentialsFile = Path.Combine(BaseDirectory, "credentials.dat");
        JobsFile = Path.Combine(BaseDirectory, "jobs.json");
        UsageLedgerFile = Path.Combine(BaseDirectory, "usage-ledger.json");
        LogsDirectory = Path.Combine(BaseDirectory, "logs");
        TempDirectory = Path.Combine(BaseDirectory, "temp");
    }

    public string BaseDirectory { get; }

    public string SettingsFile { get; }

    public string CredentialsFile { get; }

    public string JobsFile { get; }

    public string UsageLedgerFile { get; }

    public string LogsDirectory { get; }

    public string TempDirectory { get; }

    public void EnsureDirectories()
    {
        Directory.CreateDirectory(BaseDirectory);
        Directory.CreateDirectory(LogsDirectory);
        Directory.CreateDirectory(TempDirectory);
    }
}

