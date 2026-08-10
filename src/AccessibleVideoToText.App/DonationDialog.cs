namespace AccessibleVideoToText.App;

public sealed class DonationDialog : Form
{
    public DonationDialog(string imagePath)
    {
        Text = "打赏";
        AccessibleName = "打赏";
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
            Text = "如果这个工具帮到了您，可以自愿打赏。关闭窗口不会影响任何功能。",
            AutoSize = true,
            MaximumSize = new Size(520, 0),
            AccessibleName = "打赏说明"
        });

        if (File.Exists(imagePath))
        {
            using var source = Image.FromFile(imagePath);
            layout.Controls.Add(new PictureBox
            {
                Image = new Bitmap(source),
                SizeMode = PictureBoxSizeMode.Zoom,
                Size = new Size(360, 360),
                AccessibleName = "打赏二维码",
                AccessibleDescription = "可使用手机扫码，自愿打赏。"
            });
        }
        else
        {
            layout.Controls.Add(new Label
            {
                Text = "打赏二维码文件缺失，请重新安装软件。",
                AutoSize = true,
                AccessibleName = "二维码缺失提示"
            });
        }

        var close = new Button
        {
            Text = "关闭",
            AutoSize = true,
            DialogResult = DialogResult.OK,
            Anchor = AnchorStyles.Right,
            Margin = new Padding(0, 12, 0, 0)
        };
        layout.Controls.Add(close);
        Controls.Add(layout);
        AcceptButton = close;
        CancelButton = close;
        Shown += (_, _) => close.Focus();
    }
}
