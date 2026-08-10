namespace AccessibleVideoToText.App;

public sealed record DownloadChoiceOption(string Value, string Label)
{
    public override string ToString() => Label;
}

public sealed class QualitySelectionDialog : Form
{
    private readonly ComboBox choices = new();

    public QualitySelectionDialog(IReadOnlyList<DownloadChoiceOption> options)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (options.Count == 0)
        {
            throw new ArgumentException("品质选择必须至少包含一个选项。", nameof(options));
        }

        Text = "选择本次下载品质";
        AccessibleName = "选择本次下载品质";
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
            ColumnCount = 1,
            RowCount = 3,
            Dock = DockStyle.Fill
        };
        layout.Controls.Add(new Label
        {
            Text = "请选择这一次下载使用的品质或格式。上下光标可选择，Enter 继续。",
            AutoSize = true,
            MaximumSize = new Size(620, 0),
            AccessibleName = "品质选择说明"
        });

        choices.DropDownStyle = ComboBoxStyle.DropDownList;
        choices.AccessibleName = "本次下载品质或格式";
        choices.Width = 420;
        choices.Items.AddRange(options.Cast<object>().ToArray());
        choices.SelectedIndex = 0;
        layout.Controls.Add(choices);

        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Fill,
            Margin = new Padding(0, 12, 0, 0)
        };
        var cancel = new Button
        {
            Text = "取消",
            AutoSize = true,
            DialogResult = DialogResult.Cancel
        };
        var start = new Button
        {
            Text = "继续下载",
            AutoSize = true,
            DialogResult = DialogResult.OK
        };
        buttons.Controls.Add(cancel);
        buttons.Controls.Add(start);
        layout.Controls.Add(buttons);
        Controls.Add(layout);
        AcceptButton = start;
        CancelButton = cancel;
        Shown += (_, _) => choices.Focus();
    }

    public string Preference => (choices.SelectedItem as DownloadChoiceOption)?.Value ?? "best";
}

public sealed class BatchDownloadConfirmationDialog : Form
{
    private readonly ComboBox choices = new();

    public BatchDownloadConfirmationDialog(
        string summary,
        IReadOnlyList<DownloadChoiceOption> options)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (options.Count == 0)
        {
            throw new ArgumentException("批量确认必须至少包含一个选项。", nameof(options));
        }

        Text = "确认批量下载";
        AccessibleName = "确认批量下载";
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
            ColumnCount = 1,
            RowCount = 3,
            Dock = DockStyle.Fill
        };
        layout.Controls.Add(new Label
        {
            Text = summary,
            AutoSize = true,
            MaximumSize = new Size(650, 0),
            AccessibleName = "批量扫描摘要"
        });

        choices.DropDownStyle = ComboBoxStyle.DropDownList;
        choices.AccessibleName = "批量下载方式";
        choices.Width = 420;
        choices.Items.AddRange(options.Cast<object>().ToArray());
        choices.SelectedIndex = 0;
        layout.Controls.Add(choices);

        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Fill,
            Margin = new Padding(0, 12, 0, 0)
        };
        var cancel = new Button
        {
            Text = "取消",
            AutoSize = true,
            DialogResult = DialogResult.Cancel
        };
        var start = new Button
        {
            Text = "开始下载",
            AutoSize = true,
            DialogResult = DialogResult.OK
        };
        buttons.Controls.Add(cancel);
        buttons.Controls.Add(start);
        layout.Controls.Add(buttons);
        Controls.Add(layout);
        AcceptButton = start;
        CancelButton = cancel;
        Shown += (_, _) => choices.Focus();
    }

    public string Choice => (choices.SelectedItem as DownloadChoiceOption)?.Value ?? string.Empty;
}

public sealed class GenericDownloadConfirmationDialog : Form
{
    private readonly RadioButton first = new();
    private readonly RadioButton all = new();

    public GenericDownloadConfirmationDialog(
        string title,
        int count,
        IReadOnlyCollection<string> preview,
        string inspectionError)
    {
        Text = "确认普通网站下载";
        AccessibleName = "确认普通网站下载";
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
            ColumnCount = 1,
            Dock = DockStyle.Fill
        };
        var description = count > 1
            ? $"检测到列表“{title}”，预计 {count} 个视频。请选择下载范围。"
            : string.IsNullOrWhiteSpace(inspectionError)
                ? "当前链接按单个视频下载。"
                : $"无法完整预检列表：{inspectionError} 可以明确选择只下载第一个视频。";
        layout.Controls.Add(new Label
        {
            Text = description,
            AutoSize = true,
            MaximumSize = new Size(650, 0),
            AccessibleName = "普通网站扫描摘要"
        });
        if (preview.Count > 0)
        {
            layout.Controls.Add(new TextBox
            {
                Text = string.Join(Environment.NewLine, preview),
                Multiline = true,
                ReadOnly = true,
                ScrollBars = ScrollBars.Vertical,
                Size = new Size(620, 130),
                AccessibleName = "列表预览"
            });
        }

        first.Text = "只下载第一个视频";
        first.AutoSize = true;
        first.Checked = true;
        layout.Controls.Add(first);
        if (count > 1)
        {
            all.Text = $"下载全部 {count} 个视频";
            all.AutoSize = true;
            layout.Controls.Add(all);
        }

        var buttons = new FlowLayoutPanel
        {
            AutoSize = true,
            FlowDirection = FlowDirection.RightToLeft,
            Dock = DockStyle.Fill,
            Margin = new Padding(0, 12, 0, 0)
        };
        var cancel = new Button
        {
            Text = "取消",
            AutoSize = true,
            DialogResult = DialogResult.Cancel
        };
        var start = new Button
        {
            Text = "继续下载",
            AutoSize = true,
            DialogResult = DialogResult.OK
        };
        buttons.Controls.Add(cancel);
        buttons.Controls.Add(start);
        layout.Controls.Add(buttons);
        Controls.Add(layout);
        AcceptButton = start;
        CancelButton = cancel;
        Shown += (_, _) => first.Focus();
    }

    public string PlaylistMode => all.Checked ? "all" : "single";
}
