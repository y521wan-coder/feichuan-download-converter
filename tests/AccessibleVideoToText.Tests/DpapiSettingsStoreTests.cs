using System.Text;
using AccessibleVideoToText.Core;
using AccessibleVideoToText.Infrastructure;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class DpapiSettingsStoreTests
{
    private string testDirectory = null!;

    [TestInitialize]
    public void Initialize()
    {
        testDirectory = Path.Combine(Path.GetTempPath(), "AccessibleVideoToText.Tests", Guid.NewGuid().ToString("N"));
    }

    [TestCleanup]
    public void Cleanup()
    {
        if (testDirectory.StartsWith(Path.Combine(Path.GetTempPath(), "AccessibleVideoToText.Tests"), StringComparison.OrdinalIgnoreCase) &&
            Directory.Exists(testDirectory))
        {
            Directory.Delete(testDirectory, recursive: true);
        }
    }

    [TestMethod]
    public async Task SaveAndLoadCredentials_UsesCurrentUserDpapiAndNoPlaintext()
    {
        var paths = new LocalDataPaths(testDirectory);
        var store = new DpapiSettingsStore(paths);
        var expected = new CloudCredentials("123456789", "AKID-TEST-ONLY-1234", "TEST-KEY-ONLY-5678");

        await store.SaveCredentialsAsync(expected, CancellationToken.None);
        var raw = await File.ReadAllBytesAsync(paths.CredentialsFile);
        var rawText = Encoding.UTF8.GetString(raw);
        var actual = await store.LoadCredentialsAsync(CancellationToken.None);

        Assert.IsNotNull(actual);
        Assert.AreEqual(expected.AppId, actual.AppId);
        Assert.AreEqual(expected.SecretId, actual.SecretId);
        Assert.AreEqual(expected.SecretKey, actual.SecretKey);
        Assert.IsFalse(rawText.Contains(expected.SecretId, StringComparison.Ordinal));
        Assert.IsFalse(rawText.Contains(expected.SecretKey, StringComparison.Ordinal));
    }

    [TestMethod]
    public async Task SaveAndLoadSettings_DoesNotRequireCredentials()
    {
        var paths = new LocalDataPaths(testDirectory);
        var store = new DpapiSettingsStore(paths);
        var expected = new AppSettings(Mp3BitrateKbps: 320);

        await store.SaveAsync(expected, CancellationToken.None);
        var actual = await store.LoadAsync(CancellationToken.None);

        Assert.AreEqual(320, actual.Mp3BitrateKbps);
        Assert.IsFalse(File.Exists(paths.CredentialsFile));
    }
}

