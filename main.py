import sys
import os
import libvirt
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog

class VMManager:
    def __init__(self, uri="qemu:///system"):
        self.conn = libvirt.open(uri)
        if self.conn is None:
            raise RuntimeError(f"Failed to open connection to {uri}")

    def list_vms(self):
        return [{
            'name': dom.name(),
            'state': 'Running' if dom.isActive() else 'Shut off'
        } for dom in self.conn.listAllDomains()]

    def list_pools(self):
        return self.conn.listStoragePools()

    def create_pool(self, name='default', path='/var/lib/libvirt/images'):
        xml = f"""
        <pool type='dir'>
          <name>{name}</name>
          <target>
            <path>{path}</path>
          </target>
        </pool>"""
        pool = self.conn.storagePoolDefineXML(xml, 0)
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        pool.build(0)
        pool.create(0)
        pool.setAutostart(True)
        return pool

    def start_vm(self, name):
        dom = self.conn.lookupByName(name)
        return dom.create() == 0

    def shutdown_vm(self, name, force=False):
        dom = self.conn.lookupByName(name)
        if not force:
            return dom.shutdown() == 0
        else:
            return dom.destroy() == 0

    def create_vm(self, name, memory_mb, vcpus, disk_gb,
                  pool_name, network="default", iso_path=None):
        # If VM already defined, undefine to apply fresh XML
        try:
            old = self.conn.lookupByName(name)
            old.undefine()
        except libvirt.libvirtError:
            pass

        # Ensure pool exists
        pools = self.list_pools()
        if pool_name not in pools:
            self.create_pool(name=pool_name)
        pool = self.conn.storagePoolLookupByName(pool_name)

        # Remove existing volume if present
        vol_name = f"{name}.qcow2"
        try:
            existing = pool.storageVolLookupByName(vol_name)
            existing.delete(0)
        except libvirt.libvirtError:
            pass

        # Create main qcow2 volume
        vol_xml = f"""
        <volume>
          <name>{vol_name}</name>
          <capacity unit='G'>{disk_gb}</capacity>
          <target><format type='qcow2'/></target>
        </volume>"""
        vol = pool.createXML(vol_xml, 0)
        disk_path = vol.path()

        # Build OS and disk XML
        os_xml = """
          <os>
            <type arch='x86_64' machine='pc'>hvm</type>
            <boot dev='hd'/>
          </os>"""
        cdrom_xml = ""
        if iso_path:
            os_xml = """
          <os>
            <type arch='x86_64' machine='pc'>hvm</type>
            <boot dev='cdrom'/>
            <boot dev='hd'/>
          </os>"""
            cdrom_xml = f"""
            <disk type='file' device='cdrom'>
              <driver name='qemu' type='raw'/>
              <source file='{iso_path}'/>
              <target dev='hdc' bus='ide'/>
              <readonly/>
            </disk>"""

        # Define domain XML with console, graphics and ACPI
        domain_xml = f"""
        <domain type='kvm'>
          <name>{name}</name>
          <memory unit='MiB'>{memory_mb}</memory>
          <vcpu>{vcpus}</vcpu>

          <features>
            <acpi/>
            <apic/>
          </features>

{os_xml}

          <devices>
            <disk type='file' device='disk'>
              <driver name='qemu' type='qcow2'/>
              <source file='{disk_path}'/>
              <target dev='vda' bus='virtio'/>
            </disk>
{cdrom_xml}
            <interface type='network'>
              <source network='{network}'/>
              <model type='virtio'/>
            </interface>

            <!-- Consola serie para virsh console -->
            <console type='pty'>
              <target type='serial' port='0'/>
            </console>

            <!-- Salida gráfica via VNC -->
            <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'/>

          </devices>
        </domain>"""

        dom = self.conn.defineXML(domain_xml)
        if dom is None:
            raise RuntimeError("Error defining VM domain")
        return dom.create() == 0

class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Libvirt VM Manager")
        self.geometry('600x500')
        self.vm_manager = VMManager()
        self._setup_ui()
        self.refresh_vms()

    def _setup_ui(self):
        self.tree = ttk.Treeview(self, columns=('name', 'state'), show='headings')
        self.tree.heading('name', text='Name')
        self.tree.heading('state', text='State')
        self.tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        for text, cmd in (
            ('Refresh', self.refresh_vms),
            ('Start VM', self.start_selected),
            ('Shutdown VM', self.shutdown_selected),
            ('Force Off', self.force_shutdown_selected),
            ('Create VM', self.create_dialog)
        ):
            ttk.Button(btn_frame, text=text, command=cmd).pack(side=tk.LEFT, padx=5)

    def refresh_vms(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        for vm in self.vm_manager.list_vms():
            self.tree.insert('', tk.END, values=(vm['name'], vm['state']))

    def _action(self, action, force=False):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('Info', 'Select a VM first')
            return
        name = self.tree.item(sel[0], 'values')[0]
        try:
            if action == 'start':
                success = self.vm_manager.start_vm(name)
            else:
                success = self.vm_manager.shutdown_vm(name, force=force)
            if not success:
                raise RuntimeError(f"{action} failed for {name}")
        except Exception as e:
            messagebox.showerror('Error', str(e))
        finally:
            self.refresh_vms()

    def start_selected(self):
        self._action('start')

    def shutdown_selected(self):
        self._action('shutdown', force=False)

    def force_shutdown_selected(self):
        if messagebox.askyesno('Confirm', 'Force power off?'):
            self._action('shutdown', force=True)

    def create_dialog(self):
        name = simpledialog.askstring('Name', 'VM name:')
        if not name:
            return
        memory = simpledialog.askinteger('Memory', 'MiB RAM:', minvalue=128, maxvalue=65536)
        if memory is None:
            return
        cpus = simpledialog.askinteger('vCPU', 'vCPUs:', minvalue=1, maxvalue=16)
        if cpus is None:
            return
        disk = simpledialog.askinteger('Disk', 'Disk GiB:', minvalue=1, maxvalue=200)
        if disk is None:
            return

        pools = self.vm_manager.list_pools()
        if not pools:
            self.vm_manager.create_pool()
            pools = self.vm_manager.list_pools()

        pool = simpledialog.askstring('Pool', f"Storage pool? {pools}", initialvalue=pools[0])
        if not pool:
            return

        iso = filedialog.askopenfilename(title='Select ISO', filetypes=[('ISO files', '*.iso')])

        try:
            self.vm_manager.create_vm(
                name, memory, cpus, disk,
                pool_name=pool,
                iso_path=iso or None
            )
            messagebox.showinfo('Success', f"VM '{name}' creada.")
        except Exception as e:
            messagebox.showerror('Error', str(e))
        finally:
            self.refresh_vms()

if __name__ == '__main__':
    MainWindow().mainloop()
