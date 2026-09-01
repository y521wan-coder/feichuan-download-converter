using AccessibleVideoToText.Core;

namespace AccessibleVideoToText.App;

public sealed class SettingsDialog : Form
{
    private readonly ComboBox bitrateCombo = new();
    private readonly RadioButton sourceDirectoryOption = new();
    private readonly RadioButton customDirectoryOption = new();
    private readonly TextBox customDirectoryText = new();
    private readonly Button chooseDirectoryButton = new();
    private readonly Button restoreSourceDirectoryButton = new();

    public SettingsDialog(
        int currentBitrateKbps,
        bool cloudCredentialsConfigured,
        string outputPreference = OutputDirectoryPreference.SourceDirectory,
        string? customOutputDirectory = null)
    {
        Text = "设置";
        AccessibleName = "设置";
        StartPosition = FormStartPosition.CenterParent;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MinimizeBox = false;
        MaximizeBox = false;
        ShowInTaskbar = false;
        AutoSize = true;
        AutoSizeMode = AutoSizeMode.GrowAndShrink;
        AutoScaleMode = AutoScaleMode.Dpi;
        Padding = new Padding(12);

        var layout = new TableLayoutPanel
        {
            AutoSize = true,
            ColumnCount = 2,
            RowCount = 5
        };
        var bitrateLabel = new Label
        {
            Text = "最终 MP3 码率：",
            AutoSize = true,
            Anchor = AnchorStyles.Left
        };
        bitrateCombo.DropDownStyle = ComboBoxStyle.DropDownList;
        bitrateCombo.AccessibleName = "最终 MP3 码率";
        bitrateCombo.Items.AddRange(["128 kbps", "192 kbps（默认）", "256 kbps", "320 kbps"]);
        bitrateCombo.SelectedIndex = currentBitrateKbps switch
        {
            128 => 0,
            256 => 2,
            320 => 3,
            _ => 1
        };
        layout.Controls.Add(bitrateLabel, 0, 0);
        layout.Controls.Add(bitrateCombo, 1, 0);

        var outputGroup = BuildOutputGroup(outputPreference, customOutputDirectory);
        layout.SetColumnSpan(outputGroup, 2);
        layout.Controls.Add(outputGroup, 0, 1);

        var cloudStatus = new Label
        {
            AutoSize = true,
            MaximumSize = new Size(560, 0),
            Text = cloudCredentialsConfigured
                ? "腾讯云凭据：已使用 Windows DPAPI 为当前用户加密保存。软件不会显示或记录已保存密钥。"
                : "腾讯云凭据：尚未配置。只能填写撤销旧密钥后新建的最小权限 CAM 子账号凭据。",
            AccessibleName = "腾讯云设置状态",
            Margin = new Padding(0, 12, 0, 12)
        };
        layout.SetColumnSpan(cloudStatus, 2);
        layout.Controls.Add(cloudStatus, 0, 2);

        var dataButtons = new FlowLayoutPanel
        {
            AutoSize = true,
            FlowDirection = FlowDirection.LeftToRight,
            Dock = DockStyle.Fill
        };
        var configureCloud = new Button { Text = "配置腾讯云凭据", AutoSize = true };
        configureCloud.Click += (_, _) => Commit(SettingsDialogAction.ConfigureCloud);
        var clearData = new Button { Text = "清除本机全部数据", AutoSize = true };
        clearData.Click += (_, _) => Commit(SettingsDialogAction.ClearLocalData);
        dataButtons.Controls.AddRange([configureCloud, clearData]);
        layout.SetColumnSpan(dataButtons, 2);
        layout.Controls.Add(dataButtons, 0, 3);

        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Fill
        };
        var cancel = new Button
        {
            Text = "取消",
            AutoSize = true,
            DialogResult = DialogResult.Cancel
        };
        var save = new Button
        {
            Text = "保存",
            AutoSize = true,
            DialogResult = DialogResult.None
        };
        save.Click += (_, _) => Commit(SettingsDialogAction.Save);
        buttons.Controls.Add(cancel);
        buttons.Controls.Add(save);
        layout.SetColumnSpan(buttons, 2);
        layout.Controls.Add(buttons, 0, 4);
        Controls.Add(layout);

        AcceptButton = save;
        CancelButton = cancel;
        Shown += (_, _) => bitrateCombo.Focus();
    }

    public int Mp3BitrateKbps => bitrateCombo.SelectedIndex switch
    {
        0 => 128,
        2 => 256,
        3 => 320,
        _ => 192
    };

    public string OutputPreference => customDirectoryOption.Checked
        ? OutputDirectoryPreference.CustomDirectory
        : OutputDirectoryPreference.SourceDirectory;

    public string? CustomOutputDirectory => customDirectoryOption.Checked
        ? customDirectoryText.Text.Trim()
        : null;

    public SettingsDialogAction Action { get; private set; }

    private GroupBox BuildOutputGroup(string outputPreference, string? customOutputDirectory)
    {
        var group = new GroupBox
        {
            Text = "生成文件位置",
            AccessibleName = "生成文件位置",
            AutoSize = true,
            Dock = DockStyle.Fill,
            Padding = new Padding(10)
        };
        var layout = new TableLayoutPanel
        {
            AutoSize = true,
            Dock = DockStyle.Fill,
            ColumnCount = 2,
            RowCount = 4
        };

        sourceDirectoryOption.Text = "源文件旁边（默认）(&R)";
        sourceDirectoryOption.AccessibleName = "源文件旁边，默认";
        sourceDirectoryOption.AccessibleDescription = "每个生成的 MP3 或 TXT 保存在对应源文件所在目录。";
        sourceDirectoryOption.AutoSize = true;
        sourceDirectoryOption.Checked = !string.Equals(
            outputPreference,
            OutputDirectoryPreference.CustomDirectory,
            StringComparison.Ordinal);
        sourceDirectoryOption.CheckedChanged += (_, _) => UpdateOutputControls();
        layout.SetColumnSpan(sourceDirectoryOption, 2);
        layout.Controls.Add(sourceDirectoryOption, 0, 0);

        customDirectoryOption.Text = "统一保存到指定文件夹(&U)";
        customDirectoryOption.AccessibleName = "统一保存到指定文件夹";
        customDirectoryOption.AccessibleDescription = "所有新生成的 MP3 和 TXT 保存到同一个指定文件夹；下载原文件仍使用下载文件夹。";
        customDirectoryOption.AutoSize = true;
        customDirectoryOption.Checked = string.Equals(
            outputPreference,
            OutputDirectoryPreference.CustomDirectory,
            StringComparison.Ordinal);
        customDirectoryOption.CheckedChanged += (_, _) => UpdateOutputControls();
        layout.SetColumnSpan(customDirectoryOption, 2);
        layout.Controls.Add(customDirectoryOption, 0, 1);

        customDirectoryText.ReadOnly = true;
        customDirectoryText.Text = customOutputDirectory ?? string.Empty;
        customDirectoryText.AccessibleName = "统一结果目录路径";
        customDirectoryText.AccessibleDescription = "当前选择的统一结果目录，只读。";
        customDirectoryText.Dock = DockStyle.Fill;
        customDirectoryText.Width = 420;
        layout.Controls.Add(customDirectoryText, 0, 2);

        chooseDirectoryButton.Text = "选择文件夹(&F)";
        chooseDirectoryButton.AccessibleName = "选择统一结果文件夹";
        chooseDirectoryButton.AccessibleDescription = "打开系统文件夹选择对话框。取消不会改变当前设置。";
        chooseDirectoryButton.AutoSize = true;
        chooseDirectoryButton.Click += (_, _) => ChooseCustomDirectory();
        layout.Controls.Add(chooseDirectoryButton, 1, 2);

        restoreSourceDirectoryButton.Text = "恢复源文件旁边(&D)";
        restoreSourceDirectoryButton.AccessibleName = "恢复为源文件旁边";
        restoreSourceDirectoryButton.AccessibleDescription = "恢复默认输出位置；不会移动或删除已经生成的文件。";
        restoreSourceDirectoryButton.AutoSize = true;
        restoreSourceDirectoryButton.Click += (_, _) =>
        {
            sourceDirectoryOption.Checked = true;
            customDirectoryText.Text = string.Empty;
            sourceDirectoryOption.Focus();
        };
        layout.SetColumnSpan(restoreSourceDirectoryButton, 2);
        layout.Controls.Add(restoreSourceDirectoryButton, 0, 3);

        group.Controls.Add(layout);
        UpdateOutputControls();
        return group;
    }

    private void ChooseCustomDirectory()
    {
        using var dialog = new FolderBrowserDialog
        {
            Description = "选择统一结果目录。新生成的 MP3 和 TXT 将保存到这里；下载原文件仍使用下载文件夹。",
            UseDescriptionForTitle = true,
            ShowNewFolderButton = true,
            SelectedPath = Directory.Exists(customDirectoryText.Text)
                ? customDirectoryText.Text
                : string.Empty
        };
        if (dialog.ShowDialog(this) != DialogResult.OK)
        {
            chooseDirectoryButton.Focus();
            return;
        }

        customDirectoryText.Text = dialog.SelectedPath;
        customDirectoryOption.Checked = true;
        chooseDirectoryButton.Focus();
    }

    private void UpdateOutputControls()
    {
        var enabled = customDirectoryOption.Checked;
        customDirectoryText.Enabled = enabled;
        chooseDirectoryButton.Enabled = enabled;
    }

    private void Commit(SettingsDialogAction action)
    {
        if (action == SettingsDialogAction.Save && customDirectoryOption.Checked)
        {
            try
            {
                customDirectoryText.Text = OutputDirectoryPolicy.ValidateCustomDirectory(customDirectoryText.Text);
            }
            catch (Exception exception) when (exception is InvalidDataException or IOException or UnauthorizedAccessException)
            {
                MessageBox.Show(
                    this,
                    exception.Message.Trim(),
                    "统一结果目录不可用",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Warning);
                chooseDirectoryButton.Focus();
                return;
            }
        }

        Action = action;
        DialogResult = DialogResult.OK;
        Close();
    }
}

public enum SettingsDialogAction
{
    Save,
    ConfigureCloud,
    ClearLocalData
}
