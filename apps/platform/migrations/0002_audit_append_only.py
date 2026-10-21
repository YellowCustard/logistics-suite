from django.db import migrations

SQL = """
CREATE OR REPLACE FUNCTION platform_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE TRIGGER audit_entry_append_only
    BEFORE UPDATE OR DELETE ON platform_auditentry
    FOR EACH ROW EXECUTE FUNCTION platform_reject_mutation();
"""

REVERSE = """
DROP TRIGGER IF EXISTS audit_entry_append_only ON platform_auditentry;
DROP FUNCTION IF EXISTS platform_reject_mutation();
"""


class Migration(migrations.Migration):
    dependencies = [("platform", "0001_initial")]
    operations = [migrations.RunSQL(SQL, REVERSE)]
