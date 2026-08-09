using AccessibleVideoToText.App;
using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.Tests;

[TestClass]
public sealed class WinFormsAccessibilityBaselineTests
{
    [TestMethod]
    public void MainWindow_UsesNamedStandardControlsAndDpiScaling()
    {
        RunInSta(() =>
        {
            using var form = new MainForm();
            Assert.AreEqual("无障碍视频转文字", form.Text);
            Assert.AreEqual(AutoScaleMode.Dpi, form.AutoScaleMode);
            Assert.IsTrue(form.KeyPreview);

            var controls = Descendants(form).ToArray();
            var queue = controls.OfType<ListView>().Single();
            Assert.AreEqual(View.Details, queue.View);
            Assert.IsFalse(queue.OwnerDraw);
            Assert.AreEqual("文件队列", queue.AccessibleName);
            Assert.IsFalse(queue.HideSelection);

            var progress = controls.OfType<ProgressBar>().Single();
            Assert.AreEqual("当前任务进度", progress.AccessibleName);
            var results = controls.OfType<TextBox>().Single(textBox => textBox.Multiline && textBox.ReadOnly);
            Assert.AreEqual("本批结果", results.AccessibleName);

            foreach (var button in controls.OfType<Button>())
            {
                Assert.IsFalse(string.IsNullOrWhiteSpace(button.AccessibleName ?? button.Text));
                Assert.IsNull(button.Image, $"按钮 {button.Text} 不得使用图片作为操作。 ");
            }
        });
    }

    [TestMethod]
    public void ConfirmationAndCancellationDialogs_HaveSafeDefaultButtons()
    {
        RunInSta(() =>
        {
            using var paste = new PasteConfirmationDialog([
                new QueueItem(@"D:\媒体\一.mp4", MediaKind.Video),
                new QueueItem(@"D:\媒体\二.mp3", MediaKind.Mp3)]);
            Assert.IsNotNull(paste.AcceptButton);
            Assert.AreEqual("处理全部", ((Button)paste.AcceptButton).Text);
            Assert.IsNotNull(paste.CancelButton);

            using var cancellation = new CancellationScopeDialog(cloudTaskAlreadySubmitted: false);
            Assert.IsNotNull(cancellation.AcceptButton);
            Assert.AreEqual("继续处理", ((Button)cancellation.AcceptButton).Text);
            Assert.AreSame(cancellation.AcceptButton, cancellation.CancelButton);

            using var trayExit = new TrayExitDialog();
            Assert.IsNotNull(trayExit.AcceptButton);
            Assert.AreEqual("返回继续", ((Button)trayExit.AcceptButton).Text);
            Assert.AreSame(trayExit.AcceptButton, trayExit.CancelButton);
        });
    }

    [TestMethod]
    public void CloudCredentialsDialog_UsesMaskedStandardInputsAndExplicitAcknowledgement()
    {
        RunInSta(() =>
        {
            using var dialog = new CloudCredentialsDialog();
            Assert.AreEqual(AutoScaleMode.Dpi, dialog.AutoScaleMode);
            Assert.IsNotNull(dialog.AcceptButton);
            var save = (Button)dialog.AcceptButton;
            Assert.AreEqual("仅保存配置", save.Text);
            Assert.IsFalse(save.Enabled, "未确认费用和旧密钥撤销前不得保存。 ");
            Assert.IsNotNull(dialog.CancelButton);

            var controls = Descendants(dialog).ToArray();
            var secretKey = controls.OfType<TextBox>().Single(textBox => textBox.AccessibleName == "腾讯云 SecretKey");
            Assert.IsTrue(secretKey.UseSystemPasswordChar);
            Assert.IsTrue(controls.OfType<CheckBox>().Any(checkBox =>
                checkBox.Text.Contains("隐私", StringComparison.Ordinal) &&
                checkBox.Text.Contains("费用", StringComparison.Ordinal)));
            Assert.IsTrue(controls.All(control => control.GetType() != typeof(UserControl)), "云端配置必须使用标准 WinForms 控件。 ");
        });
    }

    private static IEnumerable<Control> Descendants(Control root)
    {
        foreach (Control child in root.Controls)
        {
            yield return child;
            foreach (var descendant in Descendants(child))
            {
                yield return descendant;
            }
        }
    }

    private static void RunInSta(Action action)
    {
        Exception? exception = null;
        using var completed = new ManualResetEventSlim();
        var thread = new Thread(() =>
        {
            try
            {
                action();
            }
            catch (Exception caught)
            {
                exception = caught;
            }
            finally
            {
                completed.Set();
            }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        Assert.IsTrue(completed.Wait(TimeSpan.FromSeconds(15)), "STA 无障碍基线检查超时。 ");
        thread.Join();
        if (exception is not null)
        {
            throw new AssertFailedException("STA 无障碍基线检查失败。", exception);
        }
    }
}
