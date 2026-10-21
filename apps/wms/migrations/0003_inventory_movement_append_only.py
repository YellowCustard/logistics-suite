from django.db import migrations

SQL = """
CREATE TRIGGER inventory_movement_append_only
    BEFORE UPDATE OR DELETE ON wms_inventorymovement
    FOR EACH ROW EXECUTE FUNCTION platform_reject_mutation();
"""

REVERSE = "DROP TRIGGER IF EXISTS inventory_movement_append_only ON wms_inventorymovement;"


class Migration(migrations.Migration):
    dependencies = [("wms", "0002_receiving_inventory_putaway"), ("platform", "0002_audit_append_only")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
